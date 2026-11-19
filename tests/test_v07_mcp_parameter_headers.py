"""Modern routing headers come from the exposed schema, without SDK directory caching."""

import asyncio
from pathlib import Path

import pytest

from jdagent.adapters.fake import FakeApproval, FakeModelPort
from jdagent.composition import build_runtime
from jdagent.domain.events import ToolExecutionCompletedPayload
from jdagent.domain.model import ResponseCompleted, TextDelta, ToolCallCompleted
from jdagent.domain.tools import ApprovalDecision, ToolCall
from tests.test_v07_mcp_http import wire_server
from tests.test_v07_mcp_task_management import task_configuration


@pytest.mark.parametrize("version", ["2025-11-25", "2026-07-28"])
def test_tool_arguments_emit_versioned_headers_from_the_loaded_definition(
    tmp_path: Path, version: str
) -> None:
    schema: dict[str, object] = {
        "type": "object",
        "properties": {
            "target": {
                "type": "object",
                "properties": {"tenant": {"type": "string", "x-mcp-header": "Tenant"}},
            },
            "enabled": {"type": "boolean", "x-mcp-header": "Enabled"},
            "limit": {"type": "integer", "x-mcp-header": "Limit"},
            "optional": {"type": "string", "x-mcp-header": "Optional"},
        },
    }
    headers: list[dict[str, str]] = []
    with wire_server(version, "json", tool_schema=schema, parameter_headers=headers) as url:
        config = task_configuration(tmp_path, url)

        async def scenario() -> None:
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
                                {"target": {"tenant": "α example"}, "enabled": False, "limit": 0},
                            )
                        ),
                        ResponseCompleted("tool_calls"),
                    ),
                    (
                        ToolCallCompleted(ToolCall("empty", "mcp__docs__read", {})),
                        ResponseCompleted("tool_calls"),
                    ),
                    (TextDelta("done"), ResponseCompleted("stop")),
                )
            )
            runtime = build_runtime(
                config, FakeApproval(ApprovalDecision.APPROVE), model_wrapper=lambda _: model
            )
            try:
                turn = await runtime.coordinator.send("Read routing parameters")
                results = [
                    event.payload.result
                    async for event in runtime.session.read(turn.session_id)
                    if isinstance(event.payload, ToolExecutionCompletedPayload)
                    and event.payload.result.call_id in {"read", "empty"}
                ]
                assert len(results) == 2 and all(result.error_code is None for result in results)
                assert headers == [
                    {
                        "mcp-param-tenant": "=?base64?zrEgZXhhbXBsZQ==?=",
                        "mcp-param-enabled": "false",
                        "mcp-param-limit": "0",
                    }
                    if version == "2026-07-28"
                    else {},
                    {},
                ]
            finally:
                await runtime.aclose()

        asyncio.run(scenario())


@pytest.mark.parametrize("invalid", ["root", "duplicate", "number"])
def test_invalid_header_annotations_are_isolated_before_model_exposure(
    tmp_path: Path, invalid: str
) -> None:
    schema: dict[str, object] = {"type": "object"}
    if invalid == "root":
        schema["x-mcp-header"] = "Root"
    elif invalid == "duplicate":
        schema["properties"] = {
            "first": {"type": "string", "x-mcp-header": "Tenant"},
            "second": {"type": "string", "x-mcp-header": "tenant"},
        }
    else:
        schema["properties"] = {"value": {"type": "number", "x-mcp-header": "Value"}}
    methods: list[str] = []
    with wire_server(
        "2026-07-28",
        "json",
        tool_schema=schema,
        requests=methods,
        extra_tools=({"name": "small", "inputSchema": {"type": "object"}},),
    ) as url:
        config = task_configuration(tmp_path, url)
        path = config.data_paths.user_config_file
        path.write_text(
            path.read_text(encoding="utf-8").replace(
                'tools = ["read"]', 'tools = ["read", "small"]'
            ),
            encoding="utf-8",
        )

        async def scenario() -> None:
            model = FakeModelPort(scripts=((ResponseCompleted("stop"),),))
            runtime = build_runtime(
                config, FakeApproval(ApprovalDecision.APPROVE), model_wrapper=lambda _: model
            )
            try:
                session = await runtime.coordinator.create_session()
                directory = await runtime.coordinator.mcp_list(session)
                assert directory.complete
                assert {entry.name for entry in directory.entries} == {"small"}
                assert [(entry.name, entry.reason) for entry in directory.servers[0].rejected] == [
                    ("read", "invalid_header_annotation")
                ]
                assert "tools/call" not in methods and not model.requests
            finally:
                await runtime.aclose()

        asyncio.run(scenario())
