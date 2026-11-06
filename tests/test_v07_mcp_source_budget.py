"""Long source identities must not bypass the result-preview budget."""

import asyncio
import hashlib
import json
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from jdagent.adapters.fake import FakeApproval
from jdagent.composition import build_runtime
from jdagent.domain.events import MCPInvocationFinishedPayload
from jdagent.domain.json import require_object
from jdagent.domain.model import (
    ModelCapabilities,
    ModelEvent,
    ModelRequest,
    ResponseCompleted,
    ToolCallCompleted,
)
from jdagent.domain.tools import ApprovalDecision, ToolCall
from tests.test_v07_mcp_http import wire_server
from tests.test_v07_mcp_task_management import task_configuration


@pytest.mark.parametrize("engine", ["native", "langgraph"])
def test_long_resource_identity_is_bounded_in_preview_and_readback_but_preserved_in_history(
    tmp_path: Path, engine: str
) -> None:
    uri = "docs://" + "a" * 20000

    class Reader:
        capabilities = ModelCapabilities(context_window=128000)

        def __init__(self) -> None:
            self.requests: list[ModelRequest] = []

        async def stream(self, request: ModelRequest) -> AsyncIterator[ModelEvent]:
            self.requests.append(request)
            if len(self.requests) == 1:
                call = ToolCall("long-uri", "read_mcp_resource", {"server_id": "docs", "uri": uri})
            else:
                message = request.messages[-1]
                assert len(message.content.encode("utf-8")) <= 12000
                data = json.loads(message.content)["data"]
                assert data["source"]["kind"] == "mcp"
                assert data["source"]["raw_name_sha256"] == hashlib.sha256(uri.encode()).hexdigest()
                assert data["source"]["details_in_history"] is True
                if len(self.requests) == 3:
                    assert data["next_offset"] == 200
                    yield ResponseCompleted("stop")
                    return
                call = ToolCall(
                    "page", "read_mcp_result", {"sha256": data["original"]["sha256"], "limit": 200}
                )
            yield ToolCallCompleted(call)
            yield ResponseCompleted("tool_calls")

    with wire_server("2026-07-28", "json", materials=True, tools_capability=False) as url:
        config = task_configuration(tmp_path, url, engine)
        path = config.data_paths.user_config_file
        path.write_text(path.read_text(encoding="utf-8") + "resources = true\n", encoding="utf-8")
        model = Reader()

        async def scenario() -> None:
            runtime = build_runtime(
                config, FakeApproval(ApprovalDecision.APPROVE), model_wrapper=lambda _: model
            )
            try:
                turn = await runtime.coordinator.send("Read the resource and an original page")
                assert len(model.requests[1].messages[-1].content.encode("utf-8")) <= 12000
                assert turn.result.stop_reason.value == "completed" and len(model.requests) == 3
                facts = [
                    e.payload
                    for e in [event async for event in runtime.session.read(turn.session_id)]
                    if isinstance(e.payload, MCPInvocationFinishedPayload)
                ]
                source = require_object(facts[0].result.metadata["tool_origin"], "source")
                assert source["raw_name"] == uri
                original = json.loads(
                    (
                        config.session_directory / "objects" / str(facts[0].original_hash)
                    ).read_bytes()
                )
                assert original["contents"][0]["uri"] == uri
            finally:
                await runtime.aclose()

        asyncio.run(scenario())
