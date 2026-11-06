"""Schema and mixed-content outcomes at the real runner boundary."""

import asyncio
import json
import sys
from pathlib import Path

import pytest

from jdagent.adapters.fake import FakeApproval, FakeModelPort
from jdagent.composition import build_runtime
from jdagent.configuration import CliOverrides, resolve_configuration
from jdagent.data_paths import DataPaths
from jdagent.domain.events import MCPInvocationStartedPayload, ToolExecutionCompletedPayload
from jdagent.domain.json import JsonValue, require_object
from jdagent.domain.model import ResponseCompleted, TextDelta, ToolCallCompleted
from jdagent.domain.tools import ApprovalDecision, ToolCall, ToolResultStatus


@pytest.mark.parametrize("engine", ["native", "langgraph"])
@pytest.mark.parametrize("mode", ["schema", "business_error", "mixed"])
def test_complete_structured_data_is_validated_and_content_support_is_explicit(
    tmp_path: Path, engine: str, mode: str
) -> None:
    paths = DataPaths.for_workspace(
        tmp_path, config_root=tmp_path / "config", data_root=tmp_path / "data"
    )
    paths.user_config_file.parent.mkdir(parents=True)
    peer = Path(__file__).parent / "fixtures" / "mcp_stdio_server.py"
    fixture_mode = "catalog_once" if mode == "mixed" else "catalog_output_array"
    paths.user_config_file.write_text(
        "[memory]\nauto_consolidation = false\n"
        '[mcp.servers.docs]\nenabled = true\ntransport = "stdio"\n'
        f"command = {json.dumps(sys.executable)}\n"
        f"args = {json.dumps([str(peer), '2026-07-28', fixture_mode])}\n"
        'tools = ["read"]\n',
        encoding="utf-8",
    )
    config = resolve_configuration(
        tmp_path,
        CliOverrides(provider="fake", runtime_engine=engine),
        data_paths=paths,
        environment={},
    )
    blocks: list[JsonValue] = [
        {"type": "text", "text": "Readable text"},
        {"type": "resource", "resource": {"uri": "docs://embedded", "text": "Embedded text"}},
        {"type": "resource_link", "uri": "docs://unread", "name": "Reference"},
        {"type": "image", "data": "AAE=", "mimeType": "image/png"},
        {"type": "audio", "data": "AAE=", "mimeType": "audio/wav"},
    ]
    values: list[JsonValue] = [None] if mode == "business_error" else [[1, 2], "invalid", None]
    calls = (
        tuple(
            ToolCallCompleted(
                ToolCall(
                    f"schema-{index}",
                    "mcp__docs__read",
                    {"fixture_structured": value, "fixture_error": mode == "business_error"},
                )
            )
            for index, value in enumerate(values)
        )
        if mode != "mixed"
        else (ToolCallCompleted(ToolCall("mixed", "mcp__docs__read", {"fixture_blocks": blocks})),)
    )
    model = FakeModelPort(
        scripts=(
            (
                ToolCallCompleted(ToolCall("find", "search_tools", {"query": "read"})),
                ResponseCompleted("tool_calls"),
            ),
            (*calls, ResponseCompleted("tool_calls")),
            (TextDelta("Handled the received material."), ResponseCompleted("stop")),
        )
    )

    async def scenario() -> None:
        runtime = build_runtime(
            config, FakeApproval(ApprovalDecision.APPROVE), model_wrapper=lambda _: model
        )
        try:
            turn = await runtime.coordinator.send("Read the MCP result")
            events = [event async for event in runtime.session.read(turn.session_id)]
            results = [
                event.payload.result
                for event in events
                if isinstance(event.payload, ToolExecutionCompletedPayload)
                and event.payload.result.call_id != "find"
            ]
            assert turn.result.stop_reason.value == "completed", [
                (item.call_id, item.error_message) for item in results
            ]
            if mode != "mixed":
                assert len(results) == len(values)
                for index, result in enumerate(results):
                    assert result.outcome is not None
                    assert result.outcome.data["schema_valid"] is (
                        None if mode == "business_error" else index == 0
                    )
                    assert (
                        require_object(result.outcome.data["preview"], "preview")[
                            "structuredContent"
                        ]
                        == values[index]
                    )
                    if mode == "business_error" or index > 0:
                        assert (
                            result.status is ToolResultStatus.ERROR
                            and result.outcome.observed_failure
                        )
                    else:
                        assert result.status is ToolResultStatus.SUCCESS
                    if mode == "business_error":
                        assert result.error_message == "MCP tool reported a business failure"
            else:
                assert len(results) == 1 and results[0].status is ToolResultStatus.SUCCESS
                outcome = results[0].outcome
                assert outcome is not None
                assert outcome.data["processing_complete"] is False
                assert outcome.data["unsupported_content"] == ["audio", "image"]
                assert require_object(outcome.data["preview"], "preview")["content"] == [
                    *blocks[:3],
                    {"type": "image", "unsupported": True},
                    {"type": "audio", "unsupported": True},
                ]
                operations = [
                    event.payload.operation
                    for event in events
                    if isinstance(event.payload, MCPInvocationStartedPayload)
                ]
                assert operations == ["discovery", "tools/call"]
                assert "AAE=" not in model.requests[-1].messages[-1].content
        finally:
            await runtime.aclose()

    asyncio.run(scenario())
