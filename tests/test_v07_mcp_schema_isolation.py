"""Reject broken definitions locally while preserving usable peer capabilities."""

import asyncio
from pathlib import Path

import pytest

from jdagent.adapters.fake import FakeApproval, FakeModelPort
from jdagent.composition import build_runtime
from jdagent.domain.events import ToolExecutionCompletedPayload
from jdagent.domain.json import JsonObject
from jdagent.domain.model import ResponseCompleted, ToolCallCompleted
from jdagent.domain.tools import ApprovalDecision, ToolCall
from tests.test_v07_mcp_http import wire_server
from tests.test_v07_mcp_task_management import task_configuration


@pytest.mark.parametrize("version", ["2025-11-25", "2026-07-28"])
@pytest.mark.parametrize(
    ("schema", "accepted"),
    [
        ({"type": "object", "properties": {"value": {"$ref": "#/$defs/missing"}}}, False),
        ({"type": "object", "properties": {"value": {"$ref": "#missing"}}}, False),
        ({"type": "object", "properties": {"value": {"$ref": "#/type"}}}, False),
        (
            {"type": "object", "$defs": {"child": {"$id": "child", "$ref": "#missing"}}},
            False,
        ),
        (
            {
                "type": "object",
                "const": {"$ref": "#missing"},
                "properties": {"value": {"$ref": "#/const"}},
            },
            False,
        ),
        ({"type": "object", "$dynamicRef": "#missing"}, False),
        (
            {
                "type": "object",
                "$defs": {"a/b": {"type": "string"}},
                "properties": {"value": {"$ref": "#/$defs/a~1b"}},
            },
            True,
        ),
        (
            {
                "type": "object",
                "$defs": {"text": {"$anchor": "text", "type": "string"}},
                "properties": {"value": {"$ref": "#text"}},
            },
            True,
        ),
        ({"type": "object", "properties": {"child": {"$ref": "#"}}}, True),
        ({"type": "object", "const": {"$ref": "#missing"}}, True),
    ],
)
def test_directory_resolves_local_references_before_exposing_tools(
    tmp_path: Path, version: str, schema: JsonObject, accepted: bool
) -> None:
    with wire_server(version, "json", tool_schema=dict(schema)) as url:
        config = task_configuration(tmp_path, url, "native")

        async def scenario() -> None:
            runtime = build_runtime(config, FakeApproval(ApprovalDecision.APPROVE))
            try:
                turn = await runtime.coordinator.send("hello")
                catalog = await runtime.coordinator.mcp_list(turn.session_id)
                assert catalog.complete
                assert [entry.name for entry in catalog.entries] == (["read"] if accepted else [])
                rejected = catalog.servers[0].rejected
                assert [(entry.name, entry.reason) for entry in rejected] == (
                    [] if accepted else [("read", "unsupported_input_schema")]
                )
            finally:
                await runtime.aclose()

        asyncio.run(scenario())


@pytest.mark.parametrize("version", ["2025-11-25", "2026-07-28"])
@pytest.mark.parametrize("fault", ["primitive", "schema_type", "duplicate", "envelope", "cursor"])
def test_unidentifiable_entries_duplicates_and_invalid_envelopes_keep_distinct_outcomes(
    tmp_path: Path, version: str, fault: str
) -> None:
    valid: JsonObject = {"name": "read", "inputSchema": {"type": "object"}}
    overrides: dict[str, object] = {"tools": [valid, 42]}
    if fault == "schema_type":
        overrides = {"tools": [valid, {"name": "broken", "inputSchema": []}]}
    elif fault == "duplicate":
        overrides = {"tools": [valid, {**valid, "description": 42}]}
    elif fault == "envelope":
        overrides = {"tools": 42}
    elif fault == "cursor":
        overrides = {"tools": [valid], "nextCursor": 42}
    with wire_server(version, "json", directory_overrides={"tools/list": overrides}) as url:
        config = task_configuration(tmp_path, url, "native")
        path = config.data_paths.user_config_file
        path.write_text(
            path.read_text(encoding="utf-8").replace(
                'tools = ["read"]', 'tools = ["read", "broken"]'
            ),
            encoding="utf-8",
        )

        async def scenario() -> None:
            runtime = build_runtime(config, FakeApproval(ApprovalDecision.APPROVE))
            try:
                session = await runtime.coordinator.create_session()
                catalog = await runtime.coordinator.mcp_list(session)
                if fault in {"envelope", "cursor"}:
                    assert not catalog.complete and catalog.servers[0].reason == "protocol"
                    assert not catalog.entries
                else:
                    assert catalog.complete
                    assert [entry.name for entry in catalog.entries] == (
                        [] if fault == "duplicate" else ["read"]
                    )
                    assert [entry.reason for entry in catalog.servers[0].rejected] == [
                        "duplicate_name" if fault == "duplicate" else "invalid_definition"
                    ]
            finally:
                await runtime.aclose()

        asyncio.run(scenario())


@pytest.mark.parametrize("version", ["2025-11-25", "2026-07-28"])
@pytest.mark.parametrize("engine", ["native", "langgraph"])
@pytest.mark.parametrize(
    ("method", "key", "valid", "broken"),
    [
        (
            "tools/list",
            "tools",
            {"name": "read", "inputSchema": {"type": "object"}},
            {"name": "broken", "description": 42, "inputSchema": {"type": "object"}},
        ),
        (
            "resources/list",
            "resources",
            {"name": "manual", "uri": "docs://manual"},
            {"name": "broken", "uri": 42},
        ),
        (
            "resources/templates/list",
            "resourceTemplates",
            {"name": "page", "uriTemplate": "docs://{page}"},
            {"name": "broken"},
        ),
        ("prompts/list", "prompts", {"name": "explain"}, {"name": "broken", "arguments": 42}),
    ],
)
def test_malformed_entry_does_not_hide_healthy_capabilities_or_prevent_execution(
    tmp_path: Path,
    version: str,
    engine: str,
    method: str,
    key: str,
    valid: JsonObject,
    broken: JsonObject,
) -> None:
    with wire_server(
        version,
        "json",
        tool_schema={"type": "object"},
        materials=True,
        directory_overrides={method: {key: [valid, broken]}},
    ) as url:
        config = task_configuration(tmp_path, url, engine)
        path = config.data_paths.user_config_file
        path.write_text(
            path.read_text(encoding="utf-8").replace(
                'tools = ["read"]', 'tools = ["read", "broken"]\nresources = true\nprompts = true'
            ),
            encoding="utf-8",
        )

        async def scenario() -> None:
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
                    (ResponseCompleted("stop"),),
                )
            )
            runtime = build_runtime(
                config, FakeApproval(ApprovalDecision.APPROVE), model_wrapper=lambda _: model
            )
            try:
                session = await runtime.coordinator.create_session()
                catalog = await runtime.coordinator.mcp_list(session)
                assert catalog.complete
                assert {entry.name for entry in catalog.entries} == {
                    "read",
                    "manual",
                    "page",
                    "explain",
                }
                assert [(entry.name, entry.reason) for entry in catalog.servers[0].rejected] == [
                    ("broken", "invalid_definition")
                ]
                await runtime.coordinator.send("Read", session_id=session)
                results = [
                    event.payload.result
                    async for event in runtime.session.read(session)
                    if isinstance(event.payload, ToolExecutionCompletedPayload)
                ]
                assert len(results) == 2 and all(result.error_code is None for result in results)
            finally:
                await runtime.aclose()

        asyncio.run(scenario())
