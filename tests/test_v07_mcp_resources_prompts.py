"""Resources and Prompts through the same composed model/tool boundary as ordinary tools."""

import asyncio
import json
import sys
from collections.abc import Generator
from pathlib import Path

import pytest

from jdagent.adapters.fake import FakeApproval, FakeModelPort
from jdagent.composition import build_runtime
from jdagent.configuration import CliOverrides, resolve_configuration
from jdagent.data_paths import DataPaths
from jdagent.domain.events import ToolExecutionCompletedPayload, UserMessagePayload
from jdagent.domain.json import JsonValue, require_array, require_object
from jdagent.domain.model import MessageRole, ResponseCompleted, TextDelta, ToolCallCompleted
from jdagent.domain.tools import ApprovalDecision, ToolCall, ToolResultStatus
from tests.test_v07_mcp_http import wire_server


@pytest.fixture
def material_url(request: pytest.FixtureRequest, version: str) -> Generator[str | None]:
    if request.param == "stdio":
        yield None
    else:
        with wire_server(version, request.param, materials=True, tools_capability=False) as url:
            yield url


@pytest.mark.parametrize("version", ["2026-07-28", "2025-11-25"])
@pytest.mark.parametrize("material_url", ["stdio", "json", "sse"], indirect=True)
def test_model_discovers_resources_and_prompts_then_explicitly_reads_external_material(
    tmp_path: Path, version: str, material_url: str | None
) -> None:
    paths = DataPaths.for_workspace(
        tmp_path, config_root=tmp_path / "config", data_root=tmp_path / "data"
    )
    paths.user_config_file.parent.mkdir(parents=True)
    peer = Path(__file__).parent / "fixtures" / "mcp_stdio_server.py"
    transport = (
        'transport = "stdio"\n'
        f"command = {json.dumps(sys.executable)}\n"
        f"args = {json.dumps([str(peer), version, 'catalog_once'])}\n"
        if material_url is None
        else f'transport = "http"\nurl = {json.dumps(material_url)}\n'
    )
    paths.user_config_file.write_text(
        "[memory]\nauto_consolidation = false\n"
        "[mcp.servers.docs]\nenabled = true\n" + transport + "resources = true\nprompts = true\n",
        encoding="utf-8",
    )
    config = resolve_configuration(
        tmp_path,
        CliOverrides(provider="fake"),
        data_paths=paths,
        environment={},
    )
    model = FakeModelPort(
        scripts=(
            (
                ToolCallCompleted(
                    ToolCall(
                        "resources",
                        "list_mcp_resources",
                        {"server_id": "docs", "kind": "resource_template"},
                    )
                ),
                ToolCallCompleted(
                    ToolCall(
                        "prompts", "search_mcp_prompts", {"query": "explain", "server_id": "docs"}
                    )
                ),
                ResponseCompleted("tool_calls"),
            ),
            (
                ToolCallCompleted(
                    ToolCall(
                        "read",
                        "read_mcp_resource",
                        {"server_id": "docs", "uri": "file:///private/server.txt"},
                    )
                ),
                ToolCallCompleted(
                    ToolCall(
                        "prompt",
                        "get_mcp_prompt",
                        {"server_id": "docs", "name": "explain", "arguments": {"topic": "MCP"}},
                    )
                ),
                ResponseCompleted("tool_calls"),
            ),
            (TextDelta("Used the external material."), ResponseCompleted("stop")),
        )
    )

    async def scenario() -> None:
        runtime = build_runtime(
            config, FakeApproval(ApprovalDecision.APPROVE), model_wrapper=lambda _: model
        )
        try:
            turn = await runtime.coordinator.send(
                "Find resources and the explain prompt, then read them"
            )
            assert turn.result.stop_reason.value == "completed"
            assert turn.result.tool_calls == 4
            events = [event async for event in runtime.session.read(turn.session_id)]
            results = {
                event.payload.result.call_id: event.payload.result
                for event in events
                if isinstance(event.payload, ToolExecutionCompletedPayload)
            }
            assert all(result.status is ToolResultStatus.SUCCESS for result in results.values())
            resource_list = results["resources"].outcome
            assert resource_list is not None
            listed = require_array(
                require_object(resource_list.data["preview"], "preview")["entries"], "entries"
            )
            first = require_object(listed[0], "entry")
            assert (
                require_object(first["definition"], "definition")["uriTemplate"] == "docs://{page}"
            )
            resource = results["read"].outcome
            prompt = results["prompt"].outcome
            assert resource is not None and prompt is not None
            contents = require_array(
                require_object(resource.data["preview"], "preview")["contents"], "contents"
            )
            assert require_object(contents[0], "content")["text"] == "Remote file material"
            assert (
                require_object(resource.data["source"], "source")["raw_name"]
                == "file:///private/server.txt"
            )
            messages = require_array(
                require_object(prompt.data["preview"], "preview")["messages"], "messages"
            )
            assert messages[0] == {
                "role": "user",
                "content": {"type": "text", "text": "External template for: MCP"},
            }
            assert (
                len([event for event in events if isinstance(event.payload, UserMessagePayload)])
                == 1
            )
            final_request = model.requests[-1]
            assert all(
                "External template" not in part.content for part in final_request.system_parts
            )
            assert all(
                message.role is MessageRole.TOOL
                for message in final_request.messages
                if "External template" in message.content
            )
        finally:
            await runtime.aclose()

    asyncio.run(scenario())


def test_prompt_requires_arguments_and_material_scope_cannot_be_bypassed(tmp_path: Path) -> None:
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
        "resources = true\nprompts = true\n"
        '[mcp.servers.closed]\nenabled = true\ntransport = "stdio"\ncommand = "must-not-start"\n',
        encoding="utf-8",
    )
    config = resolve_configuration(
        tmp_path,
        CliOverrides(provider="fake"),
        data_paths=paths,
        environment={},
    )
    model = FakeModelPort(
        scripts=(
            (
                ToolCallCompleted(
                    ToolCall("missing", "get_mcp_prompt", {"server_id": "docs", "name": "explain"})
                ),
                ToolCallCompleted(
                    ToolCall(
                        "closed-resource",
                        "read_mcp_resource",
                        {"server_id": "closed", "uri": "docs://private"},
                    )
                ),
                ToolCallCompleted(
                    ToolCall(
                        "closed-prompt",
                        "get_mcp_prompt",
                        {
                            "server_id": "closed",
                            "name": "explain",
                            "arguments": {"topic": "private"},
                        },
                    )
                ),
                ResponseCompleted("tool_calls"),
            ),
            (TextDelta("Please supply the topic."), ResponseCompleted("stop")),
        )
    )

    async def scenario() -> None:
        runtime = build_runtime(
            config, FakeApproval(ApprovalDecision.APPROVE), model_wrapper=lambda _: model
        )
        try:
            turn = await runtime.coordinator.send("Use explain without providing its topic")
            events = [event async for event in runtime.session.read(turn.session_id)]
            results = {
                event.payload.result.call_id: event.payload.result
                for event in events
                if isinstance(event.payload, ToolExecutionCompletedPayload)
            }
            missing = results["missing"]
            assert (
                missing.error_code is not None and missing.error_code.value == "invalid_arguments"
            )
            assert missing.outcome is not None and missing.outcome.data["missing_arguments"] == [
                "topic"
            ]
            for name in ("closed-resource", "closed-prompt"):
                code = results[name].error_code
                assert code is not None and code.value == "tool_unavailable"
            assert all(event.event_type.value != "mcp_invocation_started" for event in events)
            statuses = runtime.coordinator.mcp_status(turn.session_id)
            assert (
                next(item for item in statuses if item.server_id == "closed").connection
                == "not_connected"
            )
        finally:
            await runtime.aclose()

    asyncio.run(scenario())


@pytest.mark.parametrize("version", ["2026-07-28", "2025-11-25"])
def test_structured_results_respect_the_negotiated_protocol_contract(
    tmp_path: Path, version: str
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
        f"args = {json.dumps([str(peer), version, 'catalog_once'])}\n"
        'tools = ["read"]\n',
        encoding="utf-8",
    )
    config = resolve_configuration(
        tmp_path,
        CliOverrides(provider="fake"),
        data_paths=paths,
        environment={},
    )
    values: list[JsonValue] = [{"answer": 42}, None, [1, "two"], "text", 3, True]
    model = FakeModelPort(
        scripts=(
            (
                ToolCallCompleted(ToolCall("find", "search_tools", {"query": "read"})),
                ResponseCompleted("tool_calls"),
            ),
            (
                *[
                    ToolCallCompleted(
                        ToolCall(f"read-{index}", "mcp__docs__read", {"fixture_structured": value})
                    )
                    for index, value in enumerate(values)
                ],
                ResponseCompleted("tool_calls"),
            ),
            (TextDelta("Used all JSON types."), ResponseCompleted("stop")),
        )
    )

    async def scenario() -> None:
        runtime = build_runtime(
            config, FakeApproval(ApprovalDecision.APPROVE), model_wrapper=lambda _: model
        )
        try:
            turn = await runtime.coordinator.send("Read structured results")
            results = [
                event.payload.result
                async for event in runtime.session.read(turn.session_id)
                if isinstance(event.payload, ToolExecutionCompletedPayload)
                and event.payload.result.call_id.startswith("read-")
            ]
            assert len(results) == len(values)
            if version == "2025-11-25":
                # SDK 2.3.0's negotiated legacy validator permits objects/null,
                # whereas the modern revision accepts arbitrary JSON values.
                assert results[0].status is results[1].status is ToolResultStatus.SUCCESS
                assert results[2].error_message == "MCP operation failed: protocol"
                assert results[2].stop_turn
                assert all(
                    item.error_code is not None and item.error_code.value == "not_executed"
                    for item in results[3:]
                )
                return
            assert turn.result.stop_reason.value == "completed", [
                (item.call_id, item.error_code, item.error_message) for item in results
            ]
            for result, expected in zip(results, values, strict=True):
                assert result.status is ToolResultStatus.SUCCESS
                assert result.outcome is not None
                preview = require_object(result.outcome.data["preview"], "preview")
                assert preview["structuredContent"] == expected
                assert result.outcome.data["processing_complete"] is True
                assert result.outcome.data["schema_valid"] is None
        finally:
            await runtime.aclose()

    asyncio.run(scenario())


@pytest.mark.parametrize("variant", ["binary", "mixed"])
def test_binary_resource_is_preserved_but_never_reported_as_fully_supported(
    tmp_path: Path, variant: str
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
        "resources = true\n",
        encoding="utf-8",
    )
    config = resolve_configuration(
        tmp_path,
        CliOverrides(provider="fake"),
        data_paths=paths,
        environment={},
    )
    model = FakeModelPort(
        scripts=(
            (
                ToolCallCompleted(
                    ToolCall(
                        "read",
                        "read_mcp_resource",
                        {"server_id": "docs", "uri": f"docs://manual/{variant}"},
                    )
                ),
                ResponseCompleted("tool_calls"),
            ),
            (TextDelta("Handled the supported portion."), ResponseCompleted("stop")),
        )
    )

    async def scenario() -> None:
        runtime = build_runtime(
            config, FakeApproval(ApprovalDecision.APPROVE), model_wrapper=lambda _: model
        )
        try:
            turn = await runtime.coordinator.send("Read the resource")
            results = [
                event.payload.result
                async for event in runtime.session.read(turn.session_id)
                if isinstance(event.payload, ToolExecutionCompletedPayload)
            ]
            assert len(results) == 1
            result = results[0]
            assert result.outcome is not None
            assert result.outcome.data["processing_complete"] is False
            assert result.outcome.data["unsupported_content"] == ["binary_resource"]
            assert "original" in result.outcome.data
            assert "AAE=" not in model.requests[-1].messages[-1].content
            if variant == "binary":
                assert result.error_code is not None and result.error_code.value == "unsupported"
            else:
                assert result.status is ToolResultStatus.SUCCESS
                assert "Remote file material" in model.requests[-1].messages[-1].content
        finally:
            await runtime.aclose()

    asyncio.run(scenario())
