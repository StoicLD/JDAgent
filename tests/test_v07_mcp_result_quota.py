"""Result storage accounts for immutable originals across calls and restarts."""

import asyncio
import json
import sys
import threading
from pathlib import Path

import pytest

from jdagent.adapters.fake import FakeApproval, FakeModelPort
from jdagent.composition import build_runtime
from jdagent.configuration import CliOverrides, resolve_configuration
from jdagent.data_paths import DataPaths
from jdagent.domain.events import MCPInvocationFinishedPayload, ToolExecutionCompletedPayload
from jdagent.domain.json import require_object, require_string
from jdagent.domain.model import ResponseCompleted, TextDelta, ToolCallCompleted
from jdagent.domain.tools import ApprovalDecision, ToolCall, ToolErrorCode
from tests.test_v07_mcp_http import TaskPeerState, wire_server
from tests.test_v07_mcp_task_management import leave_running_task, task_configuration


@pytest.mark.parametrize("engine", ["native", "langgraph"])
def test_identical_originals_share_capacity_across_distinct_calls_and_restart(
    tmp_path: Path, engine: str
) -> None:
    paths = DataPaths.for_workspace(
        tmp_path, config_root=tmp_path / "config", data_root=tmp_path / "data"
    )
    paths.user_config_file.parent.mkdir(parents=True)
    peer = Path(__file__).parent / "fixtures" / "mcp_stdio_server.py"
    paths.user_config_file.write_text(
        "[memory]\nauto_consolidation = false\n[mcp]\nresult_quota_mib = 5\n"
        '[mcp.servers.docs]\nenabled = true\ntransport = "stdio"\n'
        f"command = {json.dumps(sys.executable)}\n"
        f"args = {json.dumps([str(peer), '2026-07-28', 'catalog_once'])}\n"
        'tools = ["read"]\n',
        encoding="utf-8",
    )
    config = resolve_configuration(
        tmp_path,
        CliOverrides(provider="fake", runtime_engine=engine),
        data_paths=paths,
        environment={},
    )

    async def scenario() -> None:
        session_id: str | None = None
        originals: set[str] = set()
        for iteration in range(3):
            call_id = f"read-{iteration}"
            model = FakeModelPort(
                scripts=(
                    (
                        ToolCallCompleted(
                            ToolCall(f"find-{iteration}", "search_tools", {"query": "read"})
                        ),
                        ResponseCompleted("tool_calls"),
                    ),
                    (
                        ToolCallCompleted(
                            ToolCall(call_id, "mcp__docs__read", {"fixture_size": 600_000})
                        ),
                        ResponseCompleted("tool_calls"),
                    ),
                    (TextDelta("Read."), ResponseCompleted("stop")),
                )
            )
            runtime = build_runtime(
                config,
                FakeApproval(ApprovalDecision.APPROVE),
                model_wrapper=lambda _, selected=model: selected,
            )
            try:
                turn = await runtime.coordinator.send(
                    "Read the same remote original", session_id=session_id
                )
                session_id = turn.session_id
                events = [event async for event in runtime.session.read(session_id)]
                result = next(
                    event.payload.result
                    for event in events
                    if isinstance(event.payload, ToolExecutionCompletedPayload)
                    and event.payload.result.call_id == call_id
                )
                assert result.error_code is None
                original = next(
                    event.payload.original_hash
                    for event in events
                    if isinstance(event.payload, MCPInvocationFinishedPayload)
                    and event.payload.result.call_id == call_id
                )
                assert original is not None
                originals.add(original)
            finally:
                await runtime.aclose()
        assert len(originals) == 1

    asyncio.run(scenario())


@pytest.mark.parametrize("engine", ["native", "langgraph"])
@pytest.mark.parametrize("different", [False, True])
def test_task_originals_remain_charged_and_readable_after_later_results_and_restart(
    tmp_path: Path, engine: str, different: bool
) -> None:
    state = TaskPeerState(result_text="a" * 600_000)
    with wire_server("2026-07-28", "json", task_state=state) as url:
        config = task_configuration(tmp_path, url, engine)
        path = config.data_paths.user_config_file
        path.write_text(
            "[mcp]\nresult_quota_mib = 5\n" + path.read_text(encoding="utf-8"), encoding="utf-8"
        )

        async def scenario() -> None:
            session_id, task_ref = await leave_running_task(config)
            state.status = "completed"
            runtime = build_runtime(config, FakeApproval(ApprovalDecision.APPROVE))
            try:
                first = await runtime.coordinator.mcp_task(session_id, task_ref)
                first_hash = require_string(
                    require_object(first.data["original"], "Original"), "sha256"
                )
                if different:
                    state.result_text = "b" * 600_000
                second = await runtime.coordinator.mcp_task(session_id, task_ref)
                second_hash = require_string(
                    require_object(second.data["original"], "Original"), "sha256"
                )
                assert (first_hash != second_hash) is different
            finally:
                await runtime.aclose()
            model = FakeModelPort(
                scripts=(
                    (
                        ToolCallCompleted(ToolCall("find-next", "search_tools", {"query": "read"})),
                        ResponseCompleted("tool_calls"),
                    ),
                    (
                        ToolCallCompleted(ToolCall("next", "mcp__docs__read", {})),
                        ResponseCompleted("tool_calls"),
                    ),
                    (
                        ToolCallCompleted(
                            ToolCall(
                                "old-page",
                                "read_mcp_result",
                                {"sha256": first_hash, "offset": 1000, "limit": 100},
                            )
                        ),
                        ToolCallCompleted(
                            ToolCall(
                                "new-page",
                                "read_mcp_result",
                                {"sha256": second_hash, "offset": 1000, "limit": 100},
                            )
                        ),
                        ResponseCompleted("tool_calls"),
                    ),
                    (TextDelta("Read both retained originals."), ResponseCompleted("stop")),
                )
            )
            reopened = build_runtime(
                config, FakeApproval(ApprovalDecision.APPROVE), model_wrapper=lambda _: model
            )
            try:
                await reopened.coordinator.send(
                    "Read another document and both prior results", session_id=session_id
                )
                events = [event async for event in reopened.session.read(session_id)]
                results = {
                    event.payload.result.call_id: event.payload.result
                    for event in events
                    if isinstance(event.payload, ToolExecutionCompletedPayload)
                }
                assert results["next"].error_code is (ToolErrorCode.CAPACITY if different else None)
                assert state.submissions == (1 if different else 2)
                for call_id, content in (
                    ("old-page", "a" * 100),
                    ("new-page", ("b" if different else "a") * 100),
                ):
                    outcome = results[call_id].outcome
                    assert outcome is not None and outcome.data["text"] == content
            finally:
                await reopened.aclose()

        asyncio.run(scenario())


@pytest.mark.parametrize("engine", ["native", "langgraph"])
def test_interrupted_task_refetch_retains_its_reservation_after_restart(
    tmp_path: Path, engine: str
) -> None:
    state = TaskPeerState(result_text="a" * 600_000)
    arrived, release = threading.Event(), threading.Event()
    release.set()
    with wire_server(
        "2026-07-28", "json", task_state=state, task_query_gate=(arrived, release)
    ) as url:
        config = task_configuration(tmp_path, url, engine)
        path = config.data_paths.user_config_file
        path.write_text(
            "[mcp]\nresult_quota_mib = 5\n" + path.read_text(encoding="utf-8"), encoding="utf-8"
        )

        async def scenario() -> None:
            session_id, task_ref = await leave_running_task(config)
            state.status = "completed"
            runtime = build_runtime(config, FakeApproval(ApprovalDecision.APPROVE))
            try:
                await runtime.coordinator.mcp_task(session_id, task_ref)
                arrived.clear()
                release.clear()
                fetching = asyncio.create_task(runtime.coordinator.mcp_task(session_id, task_ref))
                try:
                    assert await asyncio.to_thread(arrived.wait, 5)
                finally:
                    fetching.cancel()
                    with pytest.raises(asyncio.CancelledError):
                        await fetching
            finally:
                release.set()
                await runtime.aclose()
            model = FakeModelPort(
                scripts=(
                    (
                        ToolCallCompleted(ToolCall("find-next", "search_tools", {"query": "read"})),
                        ResponseCompleted("tool_calls"),
                    ),
                    (
                        ToolCallCompleted(ToolCall("pending-quota", "mcp__docs__read", {})),
                        ResponseCompleted("tool_calls"),
                    ),
                    (TextDelta("Waiting for the original result."), ResponseCompleted("stop")),
                    (
                        ToolCallCompleted(ToolCall("released-quota", "mcp__docs__read", {})),
                        ResponseCompleted("tool_calls"),
                    ),
                    (
                        TextDelta("Read after resolving the pending fetch."),
                        ResponseCompleted("stop"),
                    ),
                )
            )
            reopened = build_runtime(
                config, FakeApproval(ApprovalDecision.APPROVE), model_wrapper=lambda _: model
            )
            try:
                await reopened.coordinator.send("Read another document", session_id=session_id)
                events = [event async for event in reopened.session.read(session_id)]
                blocked = next(
                    event.payload.result
                    for event in events
                    if isinstance(event.payload, ToolExecutionCompletedPayload)
                    and event.payload.result.call_id == "pending-quota"
                )
                assert blocked.error_code is ToolErrorCode.CAPACITY and state.submissions == 1
                resolved = await reopened.coordinator.mcp_task(session_id, task_ref)
                assert resolved.error_code is None
                await reopened.coordinator.send("Read now", session_id=session_id)
                events = [event async for event in reopened.session.read(session_id)]
                accepted = next(
                    event.payload.result
                    for event in events
                    if isinstance(event.payload, ToolExecutionCompletedPayload)
                    and event.payload.result.call_id == "released-quota"
                )
                assert accepted.error_code is None and state.submissions == 2
            finally:
                await reopened.aclose()

        asyncio.run(scenario())


@pytest.mark.parametrize("engine", ["native", "langgraph"])
def test_task_refetch_reserves_capacity_before_rpc_but_local_result_and_cancel_remain_available(
    tmp_path: Path, engine: str
) -> None:
    state = TaskPeerState(result_text="a" * 600_000)
    with wire_server("2026-07-28", "json", task_state=state) as url:
        config = task_configuration(tmp_path, url, engine)

        async def scenario() -> None:
            session_id, task_ref = await leave_running_task(config)
            state.status = "completed"
            runtime = build_runtime(config, FakeApproval(ApprovalDecision.APPROVE))
            try:
                original = await runtime.coordinator.mcp_task(session_id, task_ref)
                path = config.data_paths.user_config_file
                path.write_text(
                    "[mcp]\nresult_quota_mib = 1\n" + path.read_text(encoding="utf-8"),
                    encoding="utf-8",
                )
                queries, cancellations = state.queries, state.cancellations
                rejected = await runtime.coordinator.mcp_task(session_id, task_ref)
                assert rejected.error_code is ToolErrorCode.CAPACITY
                assert state.queries == queries
                local = await runtime.coordinator.mcp_task(session_id, task_ref, action="result")
                assert local.data["original"] == original.data["original"]
                cancelled = await runtime.coordinator.mcp_task(
                    session_id, task_ref, action="cancel"
                )
                assert cancelled.data["cancellation_acknowledged"] is True
                assert cancelled.data["result_query_skipped"] == "capacity"
                assert state.queries == queries and state.cancellations == cancellations + 1
            finally:
                await runtime.aclose()

        asyncio.run(scenario())
