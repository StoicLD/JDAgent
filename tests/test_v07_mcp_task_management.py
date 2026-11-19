"""Explicit original-task management through the application after a restart."""

import asyncio
import json
import os
import sys
import threading
from dataclasses import asdict, replace
from pathlib import Path

import pytest

from jdagent.adapters.fake import FakeApproval, FakeModelPort
from jdagent.adapters.jsonl_recovery import JsonlRecoveryStore
from jdagent.adapters.jsonl_session import JsonlSession
from jdagent.application.session_recovery import (
    LogicalRecovery,
    SessionRecovery,
    classify_logical_recovery,
    prepare_session_resume,
)
from jdagent.composition import RuntimeOptions, build_runtime
from jdagent.configuration import CliOverrides, ResolvedConfiguration, resolve_configuration
from jdagent.data_paths import DataPaths, workspace_identity
from jdagent.domain.errors import SessionError
from jdagent.domain.events import (
    MCPTaskRegisteredPayload,
    MCPTaskResultSavedPayload,
    RuntimeEvent,
    ToolExecutionCompletedPayload,
    ToolExecutionStartedPayload,
)
from jdagent.domain.json import require_object, require_string
from jdagent.domain.model import ResponseCompleted, TextDelta, ToolCallCompleted
from jdagent.domain.tools import ApprovalDecision, ToolCall, ToolErrorCode
from jdagent.mcp_execution import MCPResultLimits
from jdagent.mcp_task_management import MCPTaskAction
from tests.test_v07_mcp_http import TaskPeerState, wire_server


def task_configuration(tmp_path: Path, url: str) -> ResolvedConfiguration:
    paths = DataPaths.for_workspace(
        tmp_path, config_root=tmp_path / "config", data_root=tmp_path / "data"
    )
    paths.user_config_file.parent.mkdir(parents=True)
    paths.user_config_file.write_text(
        "[memory]\nauto_consolidation = false\n"
        '[mcp.servers.docs]\nenabled = true\ntransport = "http"\n'
        f"url = {json.dumps(url)}\n"
        'tools = ["read"]\n',
        encoding="utf-8",
    )
    return resolve_configuration(
        tmp_path,
        CliOverrides(provider="fake"),
        data_paths=paths,
        environment={},
    )


async def leave_running_task(config: ResolvedConfiguration) -> tuple[str, str]:
    registered = asyncio.Event()

    class ObserveRegistration:
        async def observe(self, event: RuntimeEvent) -> None:
            if isinstance(event.payload, MCPTaskRegisteredPayload):
                registered.set()

    model = FakeModelPort(
        scripts=(
            (
                ToolCallCompleted(ToolCall("find", "search_tools", {"query": "read"})),
                ResponseCompleted("tool_calls"),
            ),
            (
                ToolCallCompleted(
                    ToolCall("remote", "mcp__docs__read", {"fixture_task": "cancel_ignored"})
                ),
                ResponseCompleted("tool_calls"),
            ),
        )
    )
    runtime = build_runtime(
        config,
        FakeApproval(ApprovalDecision.APPROVE),
        model_wrapper=lambda _: model,
        event_observers=(ObserveRegistration(),),
    )
    try:
        sending = asyncio.create_task(runtime.coordinator.send("Start a remote read"))
        try:
            await asyncio.wait_for(registered.wait(), 5)
        finally:
            sending.cancel()
            turn = await sending
        tasks = await runtime.coordinator.mcp_tasks(turn.session_id)
        assert len(tasks) == 1
        return turn.session_id, tasks[0].task_ref
    finally:
        await runtime.aclose()


def test_management_cancellation_after_terminal_receipt_preserves_result_across_restart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = TaskPeerState()
    with wire_server("2026-07-28", "json", task_state=state) as url:
        config = task_configuration(tmp_path, url)

        async def scenario() -> None:
            session_id, task_ref = await leave_running_task(config)
            state.status = "completed"
            arrived, release = threading.Event(), threading.Event()
            original_fsync = os.fsync
            path = config.session_directory / f"{session_id}.jsonl"

            def delayed_fsync(descriptor: int) -> None:
                original_fsync(descriptor)
                last = json.loads(path.read_bytes().splitlines()[-1])
                if (
                    not arrived.is_set()
                    and last["event_type"] == "mcp_task_observed"
                    and last["payload"].get("status") == "completed"
                ):
                    arrived.set()
                    assert release.wait(5)

            monkeypatch.setattr(os, "fsync", delayed_fsync)
            model = FakeModelPort(scripts=((ResponseCompleted("stop"),),))
            runtime = build_runtime(
                config, FakeApproval(ApprovalDecision.APPROVE), model_wrapper=lambda _: model
            )
            querying = asyncio.create_task(runtime.coordinator.mcp_task(session_id, task_ref))
            try:
                assert await asyncio.to_thread(arrived.wait, 5)
                querying.cancel()
                await asyncio.sleep(0)
                querying.cancel()
                release.set()
                outcome = await querying
                assert outcome.cancelled and outcome.requires_stop
                views = await runtime.coordinator.mcp_tasks(session_id)
                assert views[0].status == "completed" and views[0].result_available
            finally:
                release.set()
                await asyncio.gather(querying, return_exceptions=True)
                await runtime.aclose()
            requests = (state.queries, state.cancellations, state.submissions)
            reopened = build_runtime(
                config, FakeApproval(ApprovalDecision.APPROVE), model_wrapper=lambda _: model
            )
            try:
                result = await reopened.coordinator.mcp_task(session_id, task_ref, action="result")
                assert result.error_code is None and "remote task result" in json.dumps(result.data)
                assert (state.queries, state.cancellations, state.submissions) == requests
                assert not model.requests
            finally:
                await reopened.aclose()

        asyncio.run(scenario())


def test_repeated_management_queries_respect_updated_poll_hint_across_restart(
    tmp_path: Path,
) -> None:
    state = TaskPeerState()
    with wire_server("2026-07-28", "json", task_state=state) as url:
        config = task_configuration(tmp_path, url)

        async def scenario() -> None:
            session_id, task_ref = await leave_running_task(config)
            state.poll_interval_ms = 300
            model = FakeModelPort(scripts=((ResponseCompleted("stop"),),))
            # Each application instance has a fresh in-memory connection and
            # must still honor the most recently persisted server interval.
            for _ in range(3):
                runtime = build_runtime(
                    config, FakeApproval(ApprovalDecision.APPROVE), model_wrapper=lambda _: model
                )
                try:
                    outcome = await runtime.coordinator.mcp_task(session_id, task_ref)
                    assert outcome.error_code is None and outcome.data["task_status"] == "working"
                finally:
                    await runtime.aclose()
            first, second, third = state.query_times[-3:]
            assert second - first >= 0.28 and third - second >= 0.28
            assert state.submissions == 1 and not model.requests

        asyncio.run(scenario())


def test_management_retains_result_when_unsupported_input_finishes_during_cancel(
    tmp_path: Path,
) -> None:
    state = TaskPeerState()
    with wire_server("2026-07-28", "json", task_state=state) as url:
        config = task_configuration(tmp_path, url)

        async def scenario() -> None:
            session_id, task_ref = await leave_running_task(config)
            state.status, state.complete_on_cancel = "input_required", True
            model = FakeModelPort(scripts=((ResponseCompleted("stop"),),))
            runtime = build_runtime(
                config, FakeApproval(ApprovalDecision.APPROVE), model_wrapper=lambda _: model
            )
            try:
                outcome = await runtime.coordinator.mcp_task(session_id, task_ref)
                assert outcome.error_code is ToolErrorCode.UNSUPPORTED
                events = [e async for e in runtime.session.read(session_id)]
                saved = [
                    e.payload for e in events if isinstance(e.payload, MCPTaskResultSavedPayload)
                ]
                assert len(saved) == 1 and saved[0].original_hash
                assert "remote task result" in json.dumps(saved[0].outcome.data)
                requests = (state.queries, state.cancellations, state.submissions)
                result = await runtime.coordinator.mcp_task(session_id, task_ref, action="result")
                assert result.error_code is None and "remote task result" in json.dumps(result.data)
                assert (state.queries, state.cancellations, state.submissions) == requests
                assert not model.requests
            finally:
                await runtime.aclose()

        asyncio.run(scenario())


@pytest.mark.parametrize("terminal", ["failed", "cancelled", "working"])
def test_only_confirmed_terminal_management_releases_unused_business_result_reservation(
    tmp_path: Path, terminal: str
) -> None:
    state = TaskPeerState()
    with wire_server("2026-07-28", "json", task_state=state) as url:
        config = task_configuration(tmp_path, url)

        async def scenario() -> None:
            session_id, task_ref = await leave_running_task(config)
            state.status = terminal
            model = FakeModelPort(
                scripts=(
                    (
                        ToolCallCompleted(ToolCall("find-next", "search_tools", {"query": "read"})),
                        ResponseCompleted("tool_calls"),
                    ),
                    (
                        ToolCallCompleted(ToolCall("next-read", "mcp__docs__read", {})),
                        ResponseCompleted("tool_calls"),
                    ),
                    (TextDelta("The next operation completed."), ResponseCompleted("stop")),
                )
            )
            runtime = build_runtime(
                config,
                FakeApproval(ApprovalDecision.APPROVE),
                model_wrapper=lambda _: model,
                runtime_options=RuntimeOptions(
                    mcp_result_limits=MCPResultLimits(session_bytes=5 << 20)
                ),
            )
            try:
                terminal_result = await runtime.coordinator.mcp_task(session_id, task_ref)
                assert terminal_result.data["task_status"] == terminal
                state.status = "completed"
                turn = await runtime.coordinator.send(
                    "Read another document", session_id=session_id
                )
                events = [e async for e in runtime.session.read(session_id)]
                outcome = next(
                    e.payload.result
                    for e in events
                    if e.turn_id == turn.turn_id
                    and isinstance(e.payload, ToolExecutionCompletedPayload)
                    and e.payload.result.call_id == "next-read"
                )
                assert outcome.error_code is (
                    ToolErrorCode.CAPACITY if terminal == "working" else None
                )
                assert state.submissions == (1 if terminal == "working" else 2)
            finally:
                await runtime.aclose()

        asyncio.run(scenario())


@pytest.mark.parametrize("honor_cancel", [True, False])
def test_explicit_cancel_distinguishes_remote_terminal_from_acknowledgement(
    tmp_path: Path, honor_cancel: bool
) -> None:
    state = TaskPeerState()
    with wire_server("2026-07-28", "json", task_state=state) as url:
        config = task_configuration(tmp_path, url)

        async def scenario() -> None:
            session_id, task_ref = await leave_running_task(config)
            state.honor_cancel = honor_cancel
            model = FakeModelPort(scripts=((ResponseCompleted("stop"),),))
            runtime = build_runtime(
                config, FakeApproval(ApprovalDecision.APPROVE), model_wrapper=lambda _: model
            )
            try:
                outcome = await runtime.coordinator.mcp_task(session_id, task_ref, action="cancel")
                assert not outcome.completed
                assert outcome.data["cancellation_acknowledged"] is True
                assert outcome.data["task_status"] == ("cancelled" if honor_cancel else "working")
                assert outcome.cancelled is honor_cancel
                assert outcome.error_code is (ToolErrorCode.CANCELLED if honor_cancel else None)
                assert state.cancellations == 2 and state.submissions == 1 and not model.requests
            finally:
                await runtime.aclose()

        asyncio.run(scenario())


@pytest.mark.parametrize(
    "fault",
    [
        "other_workspace",
        "missing_handle",
        "corrupt_handle",
        "generation",
        "call_id",
        "server_id",
        "raw_name",
    ],
)
def test_management_validates_workspace_and_private_object_before_any_task_request(
    tmp_path: Path, fault: str
) -> None:
    state = TaskPeerState()
    with wire_server("2026-07-28", "json", task_state=state) as url:
        config = task_configuration(tmp_path, url)

        async def scenario() -> None:
            session_id, task_ref = await leave_running_task(config)
            inspect = build_runtime(config, FakeApproval(ApprovalDecision.APPROVE))
            events = [e async for e in inspect.session.read(session_id)]
            await inspect.aclose()
            registration = next(
                e.payload for e in events if isinstance(e.payload, MCPTaskRegisteredPayload)
            )
            selected = config
            if fault == "other_workspace":
                other = tmp_path / "other-workspace"
                other.mkdir()
                selected = replace(config, workspace=other)
            elif fault in {"generation", "call_id", "server_id", "raw_name"}:
                (config.session_directory / f"{session_id}.jsonl").unlink()
                altered = JsonlSession(config.session_directory)
                for event in events:
                    if isinstance(event.payload, MCPTaskRegisteredPayload):
                        event = replace(
                            event, payload=replace(event.payload, **{fault: "different-value"})
                        )
                    await altered.append(event)
            else:
                controlled = config.session_directory / "objects" / registration.handle_hash
                if fault == "missing_handle":
                    controlled.unlink()
                else:
                    controlled.write_bytes(b"corrupted handle")
            before = (state.submissions, state.queries, state.cancellations)
            runtime = build_runtime(selected, FakeApproval(ApprovalDecision.APPROVE))
            try:
                expected = (
                    SessionError if fault in {"missing_handle", "corrupt_handle"} else ValueError
                )
                with pytest.raises(expected):
                    await runtime.coordinator.mcp_task(session_id, task_ref)
                assert (state.submissions, state.queries, state.cancellations) == before
            finally:
                await runtime.aclose()

        asyncio.run(scenario())


def test_original_task_management_does_not_unlock_a_session_with_uncertain_local_write(
    tmp_path: Path,
) -> None:
    state = TaskPeerState()
    with wire_server("2026-07-28", "json", task_state=state) as url:
        config = task_configuration(tmp_path, url)

        async def scenario() -> None:
            session_id, task_ref = await leave_running_task(config)
            writer = FakeModelPort(
                scripts=(
                    (
                        ToolCallCompleted(
                            ToolCall(
                                "local-write",
                                "create_file",
                                {"path": "local.txt", "content": "written"},
                            )
                        ),
                        ResponseCompleted("tool_calls"),
                    ),
                    (ResponseCompleted("stop"),),
                )
            )
            writing = build_runtime(
                config, FakeApproval(ApprovalDecision.APPROVE), model_wrapper=lambda _: writer
            )
            try:
                await writing.coordinator.send("Create the local file", session_id=session_id)
                events = [event async for event in writing.session.read(session_id)]
                cutoff = next(
                    e.sequence
                    for e in events
                    if isinstance(e.payload, ToolExecutionStartedPayload)
                    and e.payload.call_id == "local-write"
                )
                path = writing.session.path_for(session_id)
            finally:
                await writing.aclose()
            path.write_bytes(b"".join(path.read_bytes().splitlines(keepends=True)[:cutoff]))
            unused = FakeModelPort(scripts=((ResponseCompleted("stop"),),))
            managed = build_runtime(
                config, FakeApproval(ApprovalDecision.APPROVE), model_wrapper=lambda _: unused
            )
            try:
                outcome = await managed.coordinator.mcp_task(session_id, task_ref)
                assert outcome.error_code is None and outcome.data["task_status"] == "working"
                events = [event async for event in managed.session.read(session_id)]
                assert (
                    classify_logical_recovery(tuple(events))
                    is LogicalRecovery.UNCERTAIN_SIDE_EFFECT
                )
                with pytest.raises(SessionError, match="uncertain side effects"):
                    await managed.coordinator.send("Continue", session_id=session_id)
                assert not unused.requests and state.submissions == 1
            finally:
                await managed.aclose()

        asyncio.run(scenario())


def test_management_result_after_crash_is_used_when_closing_the_original_model_call(
    tmp_path: Path,
) -> None:
    state = TaskPeerState()
    with wire_server("2026-07-28", "json", task_state=state) as url:
        config = task_configuration(tmp_path, url)

        async def scenario() -> None:
            session_id, task_ref = await leave_running_task(config)
            runtime = build_runtime(config, FakeApproval(ApprovalDecision.APPROVE))
            events = [event async for event in runtime.session.read(session_id)]
            registration = next(
                e for e in events if isinstance(e.payload, MCPTaskRegisteredPayload)
            )
            path = runtime.session.path_for(session_id)
            await runtime.aclose()
            path.write_bytes(
                b"".join(path.read_bytes().splitlines(keepends=True)[: registration.sequence])
            )
            state.status = "completed"
            managed = build_runtime(config, FakeApproval(ApprovalDecision.APPROVE))
            try:
                outcome = await managed.coordinator.mcp_task(session_id, task_ref)
                assert outcome.completed and outcome.error_code is None
                await managed.coordinator.release_session(session_id)
                resumed = await prepare_session_resume(
                    session=managed.session,
                    recovery=SessionRecovery(
                        JsonlRecoveryStore(config.session_directory, managed.session)
                    ),
                    session_id=session_id,
                    workspace_identity=workspace_identity(tmp_path),
                    allow_recovery_snapshot=True,
                )
                assert resumed.session_id == session_id
                restored = [e async for e in managed.session.read(session_id)]
                results = [
                    e.payload.result
                    for e in restored
                    if isinstance(e.payload, ToolExecutionCompletedPayload)
                    and e.payload.result.call_id == "remote"
                ]
                assert len(results) == 1 and results[0].outcome == outcome
                assert results[0].metadata["tool_origin"] == outcome.data["source"]
                assert (
                    state.submissions == 1
                    and classify_logical_recovery(tuple(restored)) is LogicalRecovery.CLEAN
                )
            finally:
                await managed.aclose()

        asyncio.run(scenario())


@pytest.mark.parametrize("action", ["query", "cancel"])
def test_stdio_restart_reports_lost_task_without_creating_replacement(
    tmp_path: Path, action: MCPTaskAction
) -> None:
    config = task_configuration(tmp_path, "http://127.0.0.1:1/mcp")
    peer = Path(__file__).parent / "fixtures" / "mcp_stdio_server.py"
    config.data_paths.user_config_file.write_text(
        "[memory]\nauto_consolidation = false\n"
        '[mcp.servers.docs]\nenabled = true\ntransport = "stdio"\n'
        f"command = {json.dumps(sys.executable)}\n"
        f"args = {json.dumps([str(peer), '2026-07-28', 'catalog_once'])}\n"
        'tools = ["read"]\n',
        encoding="utf-8",
    )

    async def scenario() -> None:
        session_id, task_ref = await leave_running_task(config)
        model = FakeModelPort(scripts=((ResponseCompleted("stop"),),))
        runtime = build_runtime(
            config, FakeApproval(ApprovalDecision.APPROVE), model_wrapper=lambda _: model
        )
        try:
            outcome = await runtime.coordinator.mcp_task(session_id, task_ref, action=action)
            assert outcome.error_code is ToolErrorCode.TOOL_UNAVAILABLE
            assert outcome.data["reason"] == "task_unavailable"
            assert outcome.data["task_status"] == "working"
            assert not model.requests
        finally:
            await runtime.aclose()

    asyncio.run(scenario())


@pytest.mark.parametrize("updated_ttl", [1, None])
def test_latest_retention_survives_restart_without_resetting_the_creation_time(
    tmp_path: Path, updated_ttl: int | None
) -> None:
    state = TaskPeerState(created_at="2020-01-01T00:00:00Z", ttl_ms=10**15)
    with wire_server("2026-07-28", "json", task_state=state) as url:
        config = task_configuration(tmp_path, url)

        async def scenario() -> None:
            session_id, task_ref = await leave_running_task(config)
            state.ttl_ms = updated_ttl
            runtime = build_runtime(config, FakeApproval(ApprovalDecision.APPROVE))
            try:
                observation = await runtime.coordinator.mcp_task(session_id, task_ref)
                assert observation.data["task_status"] == "working"
            finally:
                await runtime.aclose()
            before = (state.submissions, state.queries, state.cancellations)
            reopened = build_runtime(config, FakeApproval(ApprovalDecision.APPROVE))
            try:
                listing = await reopened.coordinator.mcp_tasks(session_id)
                assert listing[0].reference_expired is (updated_ttl is not None)
                outcome = await reopened.coordinator.mcp_task(session_id, task_ref)
                if updated_ttl is not None:
                    assert outcome.error_code is ToolErrorCode.TOOL_UNAVAILABLE
                    assert outcome.data["reason"] == "task_expired"
                    assert (state.submissions, state.queries, state.cancellations) == before
                else:
                    assert outcome.error_code is None and not outcome.completed
                    assert state.queries == before[1] + 1
                assert state.submissions == 1
            finally:
                await reopened.aclose()

        asyncio.run(scenario())


@pytest.mark.parametrize("change", ["account", "endpoint", "tool_removed", "disabled", "missing"])
def test_original_task_binding_is_checked_before_remote_management(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    state = TaskPeerState()
    with wire_server("2026-07-28", "json", task_state=state) as url:
        config = task_configuration(tmp_path, url)
        configuration_file = config.data_paths.user_config_file
        configuration_file.write_text(
            configuration_file.read_text(encoding="utf-8")
            + 'bearer_env = "JDAGENT_TASK_TEST_TOKEN"\n',
            encoding="utf-8",
        )
        monkeypatch.setenv("JDAGENT_TASK_TEST_TOKEN", "original-account-secret")

        async def scenario() -> None:
            session_id, task_ref = await leave_running_task(config)
            before = (state.queries, state.cancellations)
            text = configuration_file.read_text(encoding="utf-8")
            if change == "account":
                monkeypatch.setenv("JDAGENT_TASK_TEST_TOKEN", "different-account-secret")
            elif change == "endpoint":
                text = text.replace(url, url + "/changed")
            elif change == "tool_removed":
                text = text.replace('tools = ["read"]', "tools = []")
            elif change == "disabled":
                text = text.replace("enabled = true", "enabled = false")
            else:
                state.missing = True
            configuration_file.write_text(text, encoding="utf-8")
            runtime = build_runtime(config, FakeApproval(ApprovalDecision.APPROVE))
            try:
                listing = await runtime.coordinator.mcp_tasks(session_id)
                assert (
                    listing[0].task_ref == task_ref
                    and (state.queries, state.cancellations) == before
                )
                outcome = await runtime.coordinator.mcp_task(session_id, task_ref)
                if change == "tool_removed":
                    assert outcome.error_code is None and state.queries == before[0] + 1
                else:
                    assert outcome.error_code is ToolErrorCode.TOOL_UNAVAILABLE
                    reason = (
                        "task_unavailable"
                        if change == "missing"
                        else "configuration"
                        if change == "disabled"
                        else "identity_mismatch"
                    )
                    assert outcome.data["reason"] == reason
                    assert state.queries == before[0] + int(change == "missing")
                assert state.submissions == 1
                events = [event async for event in runtime.session.read(session_id)]
                serialized = json.dumps([asdict(e) for e in events], default=str) + json.dumps(
                    outcome.to_data()
                )
                assert all(
                    secret not in serialized
                    for secret in (
                        "private-http-task",
                        "private-task-not-found",
                        "original-account-secret",
                        "different-account-secret",
                    )
                )
            finally:
                await runtime.aclose()

        asyncio.run(scenario())


def test_reopened_session_queries_original_task_without_restarting_agent_or_repeating_tool(
    tmp_path: Path,
) -> None:
    state = TaskPeerState()
    with wire_server("2026-07-28", "json", task_state=state) as url:
        paths = DataPaths.for_workspace(
            tmp_path, config_root=tmp_path / "config", data_root=tmp_path / "data"
        )
        paths.user_config_file.parent.mkdir(parents=True)
        paths.user_config_file.write_text(
            "[memory]\nauto_consolidation = false\n"
            '[mcp.servers.docs]\nenabled = true\ntransport = "http"\n'
            f"url = {json.dumps(url)}\n"
            'tools = ["read"]\n',
            encoding="utf-8",
        )
        config = resolve_configuration(
            tmp_path,
            CliOverrides(provider="fake"),
            data_paths=paths,
            environment={},
        )

        async def scenario() -> None:
            registered = asyncio.Event()

            class ObserveRegistration:
                async def observe(self, event: RuntimeEvent) -> None:
                    if isinstance(event.payload, MCPTaskRegisteredPayload):
                        registered.set()

            model = FakeModelPort(
                scripts=(
                    (
                        ToolCallCompleted(ToolCall("find", "search_tools", {"query": "read"})),
                        ResponseCompleted("tool_calls"),
                    ),
                    (
                        ToolCallCompleted(ToolCall("remote", "mcp__docs__read", {})),
                        ResponseCompleted("tool_calls"),
                    ),
                )
            )
            runtime = build_runtime(
                config,
                FakeApproval(ApprovalDecision.APPROVE),
                model_wrapper=lambda _: model,
                event_observers=(ObserveRegistration(),),
            )
            try:
                sending = asyncio.create_task(runtime.coordinator.send("Start the remote read"))
                await asyncio.wait_for(registered.wait(), 5)
                sending.cancel()
                turn = await sending
                assert state.submissions == 1 and state.cancellations == 1
            finally:
                await runtime.aclose()

            unused_model = FakeModelPort(
                scripts=((TextDelta("must not run"), ResponseCompleted("stop")),)
            )
            reopened = build_runtime(
                config, FakeApproval(ApprovalDecision.APPROVE), model_wrapper=lambda _: unused_model
            )
            try:
                listing = await reopened.coordinator.mcp_tasks(turn.session_id)
                assert len(listing) == 1 and listing[0].status == "working"
                assert "private-http-task" not in json.dumps([asdict(item) for item in listing])
                state.status = "completed"
                outcome = await reopened.coordinator.mcp_task(
                    turn.session_id, listing[0].task_ref, action="query"
                )
                assert outcome.error_code is None and outcome.completed
                assert "remote task result" in json.dumps(outcome.data)
                queries = state.queries
                saved = await reopened.coordinator.mcp_task(
                    turn.session_id, listing[0].task_ref, action="result"
                )
                assert saved == outcome and state.queries == queries
                events = [event async for event in reopened.session.read(turn.session_id)]
                old_results = [
                    e.payload.result
                    for e in events
                    if isinstance(e.payload, ToolExecutionCompletedPayload)
                    and e.payload.result.call_id == "remote"
                ]
                assert (
                    len(old_results) == 1 and old_results[0].error_code is ToolErrorCode.CANCELLED
                )
                assert outcome.data["source"] == old_results[0].metadata["tool_origin"]
                assert classify_logical_recovery(tuple(events)) is LogicalRecovery.CLEAN
                assert len(unused_model.requests) == 0 and state.submissions == 1
                assert "private-http-task" not in json.dumps(
                    [asdict(e) for e in events], default=str
                )
            finally:
                await reopened.aclose()

            original = require_object(outcome.data.get("original"), "original")
            reader = FakeModelPort(
                scripts=(
                    (
                        ToolCallCompleted(
                            ToolCall(
                                "page",
                                "read_mcp_result",
                                {
                                    "sha256": require_string(original, "sha256"),
                                },
                            )
                        ),
                        ResponseCompleted("tool_calls"),
                    ),
                    (TextDelta("Read the saved Task result"), ResponseCompleted("stop")),
                )
            )
            reading = build_runtime(
                config, FakeApproval(ApprovalDecision.APPROVE), model_wrapper=lambda _: reader
            )
            try:
                await reading.coordinator.send("Read that result", session_id=turn.session_id)
                events = [event async for event in reading.session.read(turn.session_id)]
                page = next(
                    e.payload.result
                    for e in events
                    if isinstance(e.payload, ToolExecutionCompletedPayload)
                    and e.payload.result.call_id == "page"
                )
                assert page.error_code is None and page.outcome is not None
                assert "remote task result" in require_string(page.outcome.data, "text")
                assert page.outcome.data["source"] == outcome.data["source"]
                assert state.submissions == 1 and state.queries == queries
            finally:
                await reading.aclose()

        asyncio.run(scenario())


@pytest.mark.parametrize("fault", ["changed_id", "changed_creation", "missing_cancel"])
def test_management_rejects_substituted_handles_and_reports_rejected_cancel(
    tmp_path: Path, fault: str
) -> None:
    state = TaskPeerState()
    with wire_server("2026-07-28", "json", task_state=state) as url:
        config = task_configuration(tmp_path, url)

        async def scenario() -> None:
            session_id, task_ref = await leave_running_task(config)
            action: MCPTaskAction = "query"
            if fault == "changed_id":
                state.task_id = "another-private-task"
            elif fault == "changed_creation":
                state.created_at = "2000-01-01T00:00:00Z"
            else:
                state.missing = True
                action = "cancel"
            runtime = build_runtime(config, FakeApproval(ApprovalDecision.APPROVE))
            try:
                outcome = await runtime.coordinator.mcp_task(session_id, task_ref, action=action)
                assert outcome.error_code is ToolErrorCode.TOOL_UNAVAILABLE
                assert outcome.data["reason"] == (
                    "task_unavailable" if fault == "missing_cancel" else "protocol"
                )
                assert state.submissions == 1
                tasks = await runtime.coordinator.mcp_tasks(session_id)
                assert tasks[0].status == "working" and not tasks[0].result_available
                events = [event async for event in runtime.session.read(session_id)]
                serialized = json.dumps([asdict(e) for e in events], default=str)
                assert (
                    "another-private-task" not in serialized
                    and "private-task-not-found" not in serialized
                )
            finally:
                await runtime.aclose()

        asyncio.run(scenario())
