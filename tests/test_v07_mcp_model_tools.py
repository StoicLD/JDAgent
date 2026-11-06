import asyncio
import json
import sys
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from jdagent.adapters.fake import FakeApproval, FakeModelPort
from jdagent.composition import RuntimeOptions, build_runtime
from jdagent.configuration import CliOverrides, resolve_configuration
from jdagent.data_paths import DataPaths
from jdagent.domain.events import ToolExecutionCompletedPayload
from jdagent.domain.model import (
    ModelCapabilities,
    ModelEvent,
    ModelRequest,
    ResponseCompleted,
    TextDelta,
    ToolCallCompleted,
)
from jdagent.domain.tools import ApprovalDecision, ToolCall, ToolResultStatus
from jdagent.mcp_definitions import MCPDefinitionLimits


@pytest.mark.parametrize("engine", ["native", "langgraph"])
def test_model_search_loads_complete_definitions_on_the_next_request_and_reuses_them(
    tmp_path: Path, engine: str
) -> None:
    paths = DataPaths.for_workspace(
        tmp_path, config_root=tmp_path / "config", data_root=tmp_path / "data"
    )
    paths.user_config_file.parent.mkdir(parents=True)
    fixture = Path(__file__).parent / "fixtures" / "mcp_stdio_server.py"
    paths.user_config_file.write_text(
        "[memory]\nauto_consolidation = false\n"
        '[mcp.servers.docs]\nenabled = true\ntransport = "stdio"\n'
        f"command = {json.dumps(sys.executable)}\n"
        f"args = {json.dumps([str(fixture), '2026-07-28', 'catalog_once'])}\n"
        'tools = ["read", "search"]\n',
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
                ToolCallCompleted(
                    ToolCall(
                        "find", "search_tools", {"query": "read", "server_id": "docs", "limit": 1}
                    )
                ),
                ResponseCompleted("tool_calls"),
            ),
            (
                ToolCallCompleted(
                    ToolCall(
                        "read-remote",
                        "mcp__docs__read",
                        {
                            "path": "remote://repository/file",
                            "cwd": "/remote",
                            "args": ["/outside/workspace"],
                        },
                    )
                ),
                ResponseCompleted("tool_calls"),
            ),
            (TextDelta("Found the reader."), ResponseCompleted("stop")),
        ),
        repeat_last=True,
    )

    async def scenario() -> None:
        runtime = build_runtime(
            config, FakeApproval(ApprovalDecision.APPROVE), model_wrapper=lambda _: model
        )
        try:
            turn = await runtime.coordinator.send("Find the MCP reader")
            assert "search_tools" in {tool.name for tool in model.requests[0].tools}
            assert "mcp__docs__read" not in {tool.name for tool in model.requests[0].tools}
            tools = {tool.name: tool for tool in model.requests[1].tools}
            assert tools["mcp__docs__read"].input_schema == {"type": "object"}
            assert "mcp__docs__write" not in tools
            assert "mcp_tool_snapshot" in model.requests[1].metadata
            events = [event async for event in runtime.session.read(turn.session_id)]
            discovery = next(
                event.payload.result
                for event in events
                if isinstance(event.payload, ToolExecutionCompletedPayload)
                and event.payload.result.call_id == "find"
            )
            assert discovery.outcome is not None and "original" in discovery.outcome.data
            result = next(
                event.payload.result
                for event in events
                if isinstance(event.payload, ToolExecutionCompletedPayload)
                and event.payload.result.call_id == "read-remote"
            )
            assert result.status is ToolResultStatus.SUCCESS
            assert result.outcome is not None
            assert result.outcome.data["preview"] == {
                "resultType": "complete",
                "content": [{"type": "text", "text": "ok"}],
            }
            assert result.metadata["tool_origin"] is not None
            phases = [
                event.event_type.value
                for event in events
                if getattr(event.payload, "call_id", None) == "read-remote"
                or getattr(getattr(event.payload, "result", None), "call_id", None) == "read-remote"
            ]
            assert phases.index("mcp_invocation_started") < phases.index("mcp_invocation_finished")
            await runtime.coordinator.send("Continue using those tools", session_id=turn.session_id)
            assert "mcp__docs__read" in {tool.name for tool in model.requests[3].tools}
        finally:
            await runtime.aclose()

    asyncio.run(scenario())


@pytest.mark.parametrize("engine", ["native", "langgraph"])
def test_tool_batch_keeps_its_snapshot_while_one_slot_cache_replaces_next_request(
    tmp_path: Path, engine: str
) -> None:
    paths = DataPaths.for_workspace(
        tmp_path, config_root=tmp_path / "config", data_root=tmp_path / "data"
    )
    paths.user_config_file.parent.mkdir(parents=True)
    fixture = Path(__file__).parent / "fixtures" / "mcp_stdio_server.py"
    paths.user_config_file.write_text(
        "[memory]\nauto_consolidation = false\n[mcp.servers.docs]\nenabled = true\n"
        f'transport = "stdio"\ncommand = {json.dumps(sys.executable)}\n'
        f"args = {json.dumps([str(fixture), '2026-07-28', 'catalog_once'])}\n"
        'tools = ["read", "search"]\n',
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
                ToolCallCompleted(
                    ToolCall("find-reader", "search_tools", {"query": "read", "limit": 1})
                ),
                ToolCallCompleted(ToolCall("too-early", "mcp__docs__read", {})),
                ResponseCompleted("tool_calls"),
            ),
            (
                ToolCallCompleted(
                    ToolCall("find-searcher", "search_tools", {"query": "search", "limit": 1})
                ),
                ToolCallCompleted(ToolCall("still-reader", "mcp__docs__read", {})),
                ResponseCompleted("tool_calls"),
            ),
            (
                ToolCallCompleted(ToolCall("now-searcher", "mcp__docs__search", {})),
                ResponseCompleted("tool_calls"),
            ),
            (TextDelta("done"), ResponseCompleted("stop")),
        )
    )

    async def scenario() -> None:
        runtime = build_runtime(
            config,
            FakeApproval(ApprovalDecision.APPROVE),
            runtime_options=RuntimeOptions(mcp_definition_limits=MCPDefinitionLimits(count=1)),
            model_wrapper=lambda _: model,
        )
        try:
            turn = await runtime.coordinator.send("Find and use MCP tools")
            assert turn.result.stop_reason.value == "completed"
            events = [event async for event in runtime.session.read(turn.session_id)]
            results = {
                event.payload.result.call_id: event.payload.result
                for event in events
                if isinstance(event.payload, ToolExecutionCompletedPayload)
            }
            assert results["too-early"].status is ToolResultStatus.ERROR
            assert results["still-reader"].status is ToolResultStatus.SUCCESS
            assert results["now-searcher"].status is ToolResultStatus.SUCCESS
            assert "mcp__docs__read" in {item.name for item in model.requests[1].tools}
            assert "mcp__docs__read" not in {item.name for item in model.requests[2].tools}
            assert "mcp__docs__search" in {item.name for item in model.requests[2].tools}
        finally:
            await runtime.aclose()

    asyncio.run(scenario())


@pytest.mark.parametrize("engine", ["native", "langgraph"])
def test_result_quota_rejects_before_publishing_a_possible_send(
    tmp_path: Path, engine: str
) -> None:
    paths = DataPaths.for_workspace(
        tmp_path, config_root=tmp_path / "config", data_root=tmp_path / "data"
    )
    paths.user_config_file.parent.mkdir(parents=True)
    fixture = Path(__file__).parent / "fixtures" / "mcp_stdio_server.py"
    paths.user_config_file.write_text(
        "[memory]\nauto_consolidation = false\n[mcp]\nresult_quota_mib = 1\n"
        "[mcp.servers.docs]\nenabled = true\n"
        f'transport = "stdio"\ncommand = {json.dumps(sys.executable)}\n'
        f"args = {json.dumps([str(fixture), '2026-07-28', 'catalog_once'])}\n"
        'tools = ["read"]\n',
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
                ToolCallCompleted(ToolCall("no-quota", "mcp__docs__read", {})),
                ResponseCompleted("tool_calls"),
            ),
            (TextDelta("Storage is full"), ResponseCompleted("stop")),
            (
                ToolCallCompleted(ToolCall("now-allowed", "mcp__docs__read", {})),
                ResponseCompleted("tool_calls"),
            ),
            (TextDelta("Read after increasing storage"), ResponseCompleted("stop")),
        )
    )

    async def scenario() -> None:
        runtime = build_runtime(
            config,
            FakeApproval(ApprovalDecision.APPROVE),
            model_wrapper=lambda _: model,
        )
        try:
            turn = await runtime.coordinator.send("Read with insufficient storage")
            events = [event async for event in runtime.session.read(turn.session_id)]
            result = next(
                event.payload.result
                for event in events
                if isinstance(event.payload, ToolExecutionCompletedPayload)
                and event.payload.result.call_id == "no-quota"
            )
            assert result.error_code is not None and result.error_code.value == "capacity"
            assert all(
                not (
                    event.event_type.value == "mcp_invocation_started"
                    and getattr(event.payload, "call_id", None) == "no-quota"
                )
                for event in events
            )
            paths.user_config_file.write_text(
                paths.user_config_file.read_text(encoding="utf-8").replace(
                    "result_quota_mib = 1", "result_quota_mib = 8"
                ),
                encoding="utf-8",
            )
            await runtime.coordinator.send("Read with the larger quota", session_id=turn.session_id)
            events = [event async for event in runtime.session.read(turn.session_id)]
            allowed = next(
                event.payload.result
                for event in events
                if isinstance(event.payload, ToolExecutionCompletedPayload)
                and event.payload.result.call_id == "now-allowed"
            )
            assert allowed.status is ToolResultStatus.SUCCESS
        finally:
            await runtime.aclose()

    asyncio.run(scenario())


@pytest.mark.parametrize("window", [64000, 13000])
@pytest.mark.parametrize("engine", ["native", "langgraph"])
def test_large_result_preserves_original_and_supports_session_scoped_readback(
    tmp_path: Path, engine: str, window: int
) -> None:
    paths = DataPaths.for_workspace(
        tmp_path, config_root=tmp_path / "config", data_root=tmp_path / "data"
    )
    paths.user_config_file.parent.mkdir(parents=True)
    fixture = Path(__file__).parent / "fixtures" / "mcp_stdio_server.py"
    paths.user_config_file.write_text(
        f"model_context_window = {window}\nmax_context_tokens = {window}\n"
        "max_output_tokens = 1000\n"
        "[memory]\nauto_consolidation = false\n[mcp.servers.docs]\nenabled = true\n"
        'transport = "stdio"\n'
        f"command = {json.dumps(sys.executable)}\n"
        f"args = {json.dumps([str(fixture), '2026-07-28', 'catalog_once'])}\n"
        'tools = ["read"]\n',
        encoding="utf-8",
    )
    config = resolve_configuration(
        tmp_path,
        CliOverrides(provider="fake", runtime_engine=engine),
        data_paths=paths,
        environment={},
    )

    class ReaderModel:
        capabilities = ModelCapabilities(context_window=64000)

        def __init__(self) -> None:
            self.requests: list[ModelRequest] = []

        async def stream(self, request: ModelRequest) -> AsyncIterator[ModelEvent]:
            self.requests.append(request)
            if len(self.requests) == 1:
                call = ToolCall("find", "search_tools", {"query": "read"})
            elif len(self.requests) == 2:
                call = ToolCall("large", "mcp__docs__read", {"fixture_size": 20000})
            elif len(self.requests) == 3:
                data = json.loads(request.messages[-1].content)["data"]
                assert data["preview_complete"] is False
                assert len(request.messages[-1].content.encode()) <= 12000
                call = ToolCall(
                    "page",
                    "read_mcp_result",
                    {"sha256": data["original"]["sha256"], "offset": 15000, "limit": 200},
                )
            else:
                data = json.loads(request.messages[-1].content)["data"]
                assert data["text"] == "z" * 200
                assert data["next_offset"] == 15200
                assert data["source"]["kind"] == "mcp"
                yield TextDelta("Read the original page.")
                yield ResponseCompleted("stop")
                return
            yield ToolCallCompleted(call)
            yield ResponseCompleted("tool_calls")

    model = ReaderModel()

    async def scenario() -> None:
        runtime = build_runtime(
            config, FakeApproval(ApprovalDecision.APPROVE), model_wrapper=lambda _: model
        )
        try:
            result = await runtime.coordinator.send("Read a large result and a later page")
            assert result.result.stop_reason.value == "completed"
            assert len(model.requests) == 4
        finally:
            await runtime.aclose()

    asyncio.run(scenario())
