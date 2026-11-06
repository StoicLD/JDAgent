"""Reject broken definitions locally while preserving usable peer capabilities."""

import asyncio
from pathlib import Path

import pytest

from jdagent.adapters.fake import FakeApproval
from jdagent.composition import build_runtime
from jdagent.domain.json import JsonObject
from jdagent.domain.tools import ApprovalDecision
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
