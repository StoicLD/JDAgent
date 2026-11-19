"""Modern Tasks complete inside one sequential application tool invocation."""

import asyncio
import json
import sys
from dataclasses import asdict
from pathlib import Path

import pytest

from jdagent.adapters.fake import FakeApproval, FakeModelPort
from jdagent.composition import RuntimeOptions, build_runtime
from jdagent.configuration import CliOverrides, resolve_configuration
from jdagent.data_paths import DataPaths
from jdagent.domain.events import (
    MCPInvocationFinishedPayload,
    MCPInvocationStartedPayload,
    MCPTaskRegisteredPayload,
    RuntimeEvent,
    ToolExecutionCompletedPayload,
)
from jdagent.domain.model import ResponseCompleted, TextDelta, ToolCallCompleted
from jdagent.domain.tools import ApprovalDecision, ToolCall, ToolErrorCode, ToolResultStatus
from jdagent.mcp_execution import MCPResultLimits


@pytest.mark.parametrize(
    "case",
    [
        "normal",
        "business_error",
        "failed",
        "cancelled",
        "query_timeout",
        "query_exhausted",
        "deadline",
        "input_required",
        "cancel_race",
        "cancel_ignored",
        "cancel_stalls",
        "capacity",
        "user_cancel",
    ],
)
def test_task_is_registered_then_waited_without_exposing_its_handle_to_the_model(
    tmp_path: Path, case: str
) -> None:
    paths = DataPaths.for_workspace(
        tmp_path, config_root=tmp_path / "config", data_root=tmp_path / "data"
    )
    paths.user_config_file.parent.mkdir(parents=True)
    peer = Path(__file__).parent / "fixtures" / "mcp_stdio_server.py"
    paths.user_config_file.write_text(
        "[memory]\nauto_consolidation = false\n"
        '[mcp.servers.docs]\nenabled = true\ntransport = "stdio"\n'
        f"command = {json.dumps(sys.executable)}\n"
        f"args = {json.dumps([str(peer), '2026-07-28', 'catalog_once'])}\n"
        'tools = ["read"]\n',
        encoding="utf-8",
    )
    deadline_cases = {"deadline", "cancel_race", "cancel_ignored", "cancel_stalls"}
    if case in deadline_cases:
        with paths.user_config_file.open("a", encoding="utf-8") as stream:
            stream.write("timeout_seconds = 1\n")
    config = resolve_configuration(
        tmp_path,
        CliOverrides(provider="fake"),
        data_paths=paths,
        environment={},
    )
    model = FakeModelPort(
        scripts=(
            (
                ToolCallCompleted(ToolCall("find", "search_tools", {"query": "read"})),
                ResponseCompleted("tool_calls"),
            ),
            (
                ToolCallCompleted(
                    ToolCall(
                        "read",
                        "mcp__docs__read",
                        {
                            "fixture_task": "query_hang"
                            if case in {"deadline", "user_cancel"}
                            else case
                        },
                    )
                ),
                ResponseCompleted("tool_calls"),
            ),
            (TextDelta("Task finished."), ResponseCompleted("stop")),
        )
    )

    async def scenario() -> None:
        registered_signal = asyncio.Event()

        class ObserveRegistration:
            async def observe(self, event: RuntimeEvent) -> None:
                if isinstance(event.payload, MCPTaskRegisteredPayload):
                    registered_signal.set()

        runtime = build_runtime(
            config,
            FakeApproval(ApprovalDecision.APPROVE),
            model_wrapper=lambda _: model,
            runtime_options=RuntimeOptions(mcp_result_limits=MCPResultLimits(session_bytes=3 << 20))
            if case == "capacity"
            else None,
            event_observers=(ObserveRegistration(),),
        )
        try:
            sending = asyncio.create_task(runtime.coordinator.send("Read the document"))
            if case == "user_cancel":
                await asyncio.wait_for(registered_signal.wait(), 5)
                sending.cancel()
            turn = await sending
            events = [event async for event in runtime.session.read(turn.session_id)]
            result = next(
                event.payload.result
                for event in events
                if isinstance(event.payload, ToolExecutionCompletedPayload)
                and event.payload.result.call_id == "read"
            )
            if case == "capacity":
                assert result.error_code is ToolErrorCode.CAPACITY
                assert not any(
                    isinstance(e.payload, MCPInvocationStartedPayload)
                    and e.payload.call_id == "read"
                    for e in events
                )
                return
            if case in {"normal", "query_timeout"}:
                assert result.status is ToolResultStatus.SUCCESS
                assert f"done; calls=1; polls={3 if case == 'query_timeout' else 2}" in json.dumps(
                    result.output
                )
            else:
                assert result.status is ToolResultStatus.ERROR
                assert result.error_code is (
                    ToolErrorCode.CANCELLED
                    if case in {"cancelled", "user_cancel"}
                    else ToolErrorCode.TIMEOUT
                    if case in deadline_cases | {"query_exhausted"}
                    else ToolErrorCode.UNSUPPORTED
                    if case == "input_required"
                    else ToolErrorCode.EXECUTION_FAILED
                )
            registered = [e for e in events if e.event_type.value == "mcp_task_registered"]
            observed = [e for e in events if e.event_type.value == "mcp_task_observed"]
            assert len(registered) == 1
            if case not in deadline_cases | {"input_required", "user_cancel"}:
                assert len(observed) == (3 if case in {"query_timeout", "query_exhausted"} else 2)
            else:
                assert any(e.event_type.value == "mcp_task_cancel_requested" for e in events)
                assert result.outcome is not None
                assert result.outcome.data["cancellation_requested"] is True
                expected_status = (
                    "completed"
                    if case == "cancel_race"
                    else "working"
                    if case in {"cancel_ignored", "cancel_stalls"}
                    else "cancelled"
                )
                assert result.outcome.data["task_status"] == expected_status
                assert result.outcome.data["cancellation_acknowledged"] == (case != "cancel_stalls")
            assert registered[0].sequence < observed[0].sequence
            finish = next(
                e
                for e in events
                if isinstance(e.payload, MCPInvocationFinishedPayload)
                and e.payload.result.call_id == "read"
            )
            assert observed[-1].sequence < finish.sequence
            assert isinstance(finish.payload, MCPInvocationFinishedPayload)
            assert finish.payload.result_confirmed == (
                case not in {"query_exhausted", "cancel_ignored", "cancel_stalls"}
            )
            if case == "user_cancel":
                assert "unconfirmed" not in (result.error_message or "")
            if case == "cancel_race":
                assert finish.payload.original_hash is not None
                assert result.outcome is not None and "done; calls=1; polls=2" in json.dumps(
                    result.outcome.data
                )
            if case == "cancel_stalls":
                status = runtime.coordinator.mcp_status(turn.session_id)[0]
                assert status.directory == "failed" and status.reason == "cleanup_pending"
                assert result.duration_ms < 7000
            assert (
                len(
                    [
                        e
                        for e in events
                        if isinstance(e.payload, MCPInvocationStartedPayload)
                        and e.payload.call_id == "read"
                    ]
                )
                == 1
            )
            assert "stdio-task" not in json.dumps([asdict(e) for e in events], default=str)
            assert "private-task-failure" not in json.dumps(
                [asdict(e) for e in events], default=str
            )
            assert "private-task-query-error" not in json.dumps(
                [asdict(e) for e in events], default=str
            )
            request_text = json.dumps([asdict(r) for r in model.requests], default=str)
            assert "stdio-task" not in request_text
            if case == "normal":
                assert "done; calls=1; polls=2" in request_text
            assert turn.result.tool_calls == 2 and len(model.requests) == (
                2
                if case
                in deadline_cases
                | {"cancelled", "query_exhausted", "input_required", "user_cancel"}
                else 3
            )
            if case == "normal":
                registration = registered[0].payload
                assert isinstance(registration, MCPTaskRegisteredPayload)
                await runtime.coordinator.release_session(turn.session_id)
                reader = FakeModelPort(
                    scripts=(
                        (
                            ToolCallCompleted(
                                ToolCall(
                                    "private-handle",
                                    "read_mcp_result",
                                    {"sha256": registration.handle_hash},
                                )
                            ),
                            ResponseCompleted("tool_calls"),
                        ),
                        (
                            TextDelta("The private handle cannot be read."),
                            ResponseCompleted("stop"),
                        ),
                    )
                )
                reopened = build_runtime(
                    config, FakeApproval(ApprovalDecision.APPROVE), model_wrapper=lambda _: reader
                )
                try:
                    read_turn = await reopened.coordinator.send(
                        "Read the saved reference", session_id=turn.session_id
                    )
                    read_events = [e async for e in reopened.session.read(read_turn.session_id)]
                    denied = next(
                        e.payload.result
                        for e in read_events
                        if isinstance(e.payload, ToolExecutionCompletedPayload)
                        and e.payload.result.call_id == "private-handle"
                    )
                    assert denied.error_code is ToolErrorCode.TOOL_UNAVAILABLE
                    assert "stdio-task" not in json.dumps(
                        [asdict(r) for r in reader.requests], default=str
                    )
                finally:
                    await reopened.aclose()
        finally:
            await runtime.aclose()

    asyncio.run(scenario())
