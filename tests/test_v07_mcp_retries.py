"""Only identified remote timeouts of explicitly repeatable tools may resend."""

import asyncio
import json
import sys
from pathlib import Path

import pytest

from jdagent.adapters.fake import FakeApproval, FakeModelPort
from jdagent.composition import build_runtime
from jdagent.configuration import CliOverrides, resolve_configuration
from jdagent.data_paths import DataPaths
from jdagent.domain.events import (
    MCPAttemptFinishedPayload,
    MCPInvocationFinishedPayload,
    MCPInvocationStartedPayload,
    ToolExecutionCompletedPayload,
)
from jdagent.domain.model import ResponseCompleted, TextDelta, ToolCallCompleted
from jdagent.domain.tools import ApprovalDecision, ToolCall, ToolResultStatus
from tests.test_v07_mcp_http import wire_server


@pytest.mark.parametrize("engine", ["native", "langgraph"])
def test_safe_repeat_uses_at_most_three_sends_for_one_logical_call(
    tmp_path: Path, engine: str
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
        'tools = ["read"]\n[mcp.servers.docs.tool_overrides.read]\nsafe_repeat = true\n',
        encoding="utf-8",
    )
    config = resolve_configuration(
        tmp_path,
        CliOverrides(provider="fake", runtime_engine=engine),
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
                    ToolCall("read", "mcp__docs__read", {"fixture_timeout": "twice"})
                ),
                ResponseCompleted("tool_calls"),
            ),
            (TextDelta("Read the completed result."), ResponseCompleted("stop")),
        )
    )

    async def scenario() -> None:
        runtime = build_runtime(
            config, FakeApproval(ApprovalDecision.APPROVE), model_wrapper=lambda _: model
        )
        try:
            turn = await runtime.coordinator.send("Read the MCP document")
            events = [event async for event in runtime.session.read(turn.session_id)]
            results = [
                event.payload.result
                for event in events
                if isinstance(event.payload, ToolExecutionCompletedPayload)
            ]
            assert turn.result.stop_reason.value == "completed"
            assert len(results) == 2 and results[-1].status is ToolResultStatus.SUCCESS
            assert results[-1].outcome is not None
            assert "attempts=3" in json.dumps(results[-1].outcome.data)
            attempts = [
                event.payload.attempt
                for event in events
                if isinstance(event.payload, MCPInvocationStartedPayload)
                and event.payload.call_id == "read"
            ]
            assert attempts == [1, 2, 3]
            assert "private-server-diagnostic" not in json.dumps(
                [result.output for result in results]
            )
            assert turn.result.tool_calls == 2
        finally:
            await runtime.aclose()

    asyncio.run(scenario())


@pytest.mark.parametrize("engine", ["native", "langgraph"])
@pytest.mark.parametrize("error", ["http504", "server_timeout"])
def test_http_status_cannot_authorize_retry_without_a_server_timeout_response(
    tmp_path: Path, engine: str, error: str
) -> None:
    requests: list[str] = []
    with wire_server("2026-07-28", "json", tool_error=error, requests=requests) as url:
        paths = DataPaths.for_workspace(
            tmp_path, config_root=tmp_path / "config", data_root=tmp_path / "data"
        )
        paths.user_config_file.parent.mkdir(parents=True)
        paths.user_config_file.write_text(
            "[memory]\nauto_consolidation = false\n"
            '[mcp.servers.docs]\nenabled = true\ntransport = "http"\n'
            f"url = {json.dumps(url)}\n"
            'tools = ["read"]\n[mcp.servers.docs.tool_overrides.read]\nsafe_repeat = true\n',
            encoding="utf-8",
        )
        config = resolve_configuration(
            tmp_path,
            CliOverrides(provider="fake", runtime_engine=engine),
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
                    ToolCallCompleted(ToolCall("read", "mcp__docs__read", {})),
                    ResponseCompleted("tool_calls"),
                ),
                (TextDelta("The remote request failed."), ResponseCompleted("stop")),
            )
        )

        async def scenario() -> None:
            runtime = build_runtime(
                config, FakeApproval(ApprovalDecision.APPROVE), model_wrapper=lambda _: model
            )
            try:
                turn = await runtime.coordinator.send("Read the document")
                attempts = [
                    event.payload
                    async for event in runtime.session.read(turn.session_id)
                    if isinstance(event.payload, MCPAttemptFinishedPayload)
                ]
                assert requests.count("tools/call") == (1 if error == "http504" else 3)
                assert {item.failure_kind for item in attempts} == {
                    "remote" if error == "http504" else "server_timeout"
                }
                assert all(
                    item.response_confirmed == (error == "server_timeout") for item in attempts
                )
            finally:
                await runtime.aclose()

        asyncio.run(scenario())


@pytest.mark.parametrize("engine", ["native", "langgraph"])
@pytest.mark.parametrize(
    "case", ["exhausted", "unsafe", "text_error", "deadline", "client_wait", "successful_reads"]
)
def test_retry_conditions_and_call_identity_do_not_expand_the_send_allowance(
    tmp_path: Path, engine: str, case: str
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
        'tools = ["read"]\n[mcp.servers.docs.tool_overrides.read]\n'
        f"safe_repeat = {'false' if case == 'unsafe' else 'true'}\n"
        f"timeout_seconds = {1 if case in {'deadline', 'client_wait'} else 600}\n",
        encoding="utf-8",
    )
    config = resolve_configuration(
        tmp_path,
        CliOverrides(provider="fake", runtime_engine=engine),
        data_paths=paths,
        environment={},
    )
    mode = (
        "never"
        if case == "successful_reads"
        else "stall"
        if case == "client_wait"
        else "text_error"
        if case == "text_error"
        else "always"
    )

    def read(identity: str) -> ToolCallCompleted:
        return ToolCallCompleted(ToolCall(identity, "mcp__docs__read", {"fixture_timeout": mode}))

    batch = (
        tuple(read(f"read-{index}") for index in range(4))
        if case == "successful_reads"
        else (read("read"),)
    )
    model = FakeModelPort(
        scripts=(
            (
                ToolCallCompleted(ToolCall("find", "search_tools", {"query": "read"})),
                ResponseCompleted("tool_calls"),
            ),
            (*batch, ResponseCompleted("tool_calls")),
            (read("new-call-id"), ResponseCompleted("tool_calls"))
            if case in {"exhausted", "unsafe"}
            else (TextDelta("Handled the result."), ResponseCompleted("stop")),
        )
    )

    async def scenario() -> None:
        runtime = build_runtime(
            config, FakeApproval(ApprovalDecision.APPROVE), model_wrapper=lambda _: model
        )
        try:
            turn = await runtime.coordinator.send("Read the MCP document")
            events = [event async for event in runtime.session.read(turn.session_id)]
            sends = [
                event.payload
                for event in events
                if isinstance(event.payload, MCPInvocationStartedPayload)
                and event.payload.operation == "tools/call"
            ]
            results = [
                event.payload.result
                for event in events
                if isinstance(event.payload, ToolExecutionCompletedPayload)
                and event.payload.result.call_id != "find"
            ]
            expected = {
                "exhausted": 3,
                "unsafe": 1,
                "text_error": 1,
                "deadline": 2,
                "client_wait": 1,
                "successful_reads": 4,
            }[case]
            assert len(sends) == expected
            if case == "successful_reads":
                assert all(item.status is ToolResultStatus.SUCCESS for item in results)
                assert turn.result.stop_reason.value == "completed"
            elif case in {"exhausted", "unsafe"}:
                assert len(results) == 2
                assert (
                    results[-1].error_code is not None
                    and results[-1].error_code.value == "not_executed"
                )
                assert results[-1].stop_turn
            elif case in {"deadline", "client_wait"}:
                assert results[-1].outcome is not None
                assert results[-1].outcome.data["timeout_source"] == "client"
                assert results[-1].stop_turn and results[-1].outcome.effect == "none"
                finished = [
                    event.payload
                    for event in events
                    if isinstance(event.payload, MCPInvocationFinishedPayload)
                    and event.payload.result.call_id == "read"
                ]
                # The budget expires during backoff after two confirmed timeout replies.
                assert finished[-1].result_confirmed == (case == "deadline")
            else:
                assert results[-1].outcome is not None
                assert results[-1].outcome.observed_failure
                assert "timeout_source" not in results[-1].outcome.data
        finally:
            await runtime.aclose()

    asyncio.run(scenario())
