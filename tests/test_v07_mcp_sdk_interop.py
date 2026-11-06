"""All three capabilities interoperate with a separately running, published SDK server."""

import asyncio
import json
import subprocess
import sys
import time
from collections.abc import Generator
from pathlib import Path

import pytest

from jdagent.adapters.fake import FakeApproval, FakeModelPort
from jdagent.composition import build_runtime
from jdagent.configuration import CliOverrides, resolve_configuration
from jdagent.data_paths import DataPaths
from jdagent.domain.events import MCPPromptPreparedPayload, ToolExecutionCompletedPayload
from jdagent.domain.model import ResponseCompleted, TextDelta, ToolCallCompleted
from jdagent.domain.tools import ApprovalDecision, ToolCall

PEER = Path(__file__).parent / "fixtures" / "mcp_sdk_server.py"


@pytest.fixture
def sdk_url(request: pytest.FixtureRequest, tmp_path: Path) -> Generator[str | None]:
    if request.param == "stdio":
        yield None
        return
    ready = tmp_path / "sdk-ready.txt"
    process = subprocess.Popen(
        [sys.executable, str(PEER), request.param, str(ready)],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    try:
        deadline = time.monotonic() + 15
        while not ready.exists() and process.poll() is None and time.monotonic() < deadline:
            time.sleep(0.02)
        if not ready.exists():
            if process.poll() is None:
                process.kill()
            _, stderr = process.communicate(timeout=5)
            pytest.fail("SDK server did not start: " + stderr[-4000:].decode(errors="replace"))
        yield ready.read_text(encoding="utf-8")
    finally:
        if process.poll() is None:
            process.terminate()
        process.communicate(timeout=5)


@pytest.mark.parametrize("engine", ["native", "langgraph"])
@pytest.mark.parametrize("sdk_url", ["stdio", "json", "sse"], indirect=True)
def test_sdk_server_tools_resources_and_prompts_share_the_real_runtime(
    tmp_path: Path, engine: str, sdk_url: str | None
) -> None:
    paths = DataPaths.for_workspace(
        tmp_path, config_root=tmp_path / "config", data_root=tmp_path / "data"
    )
    paths.user_config_file.parent.mkdir(parents=True)
    transport = (
        f'transport = "stdio"\ncommand = {json.dumps(sys.executable)}\n'
        f"args = {json.dumps([str(PEER), 'stdio'])}\n"
        if sdk_url is None
        else f'transport = "http"\nurl = {json.dumps(sdk_url)}\n'
    )
    paths.user_config_file.write_text(
        "[memory]\nauto_consolidation = false\n"
        "[mcp.servers.official]\nenabled = true\n"
        + transport
        + 'tools = ["echo"]\nresources = true\nprompts = true\n',
        encoding="utf-8",
    )
    config = resolve_configuration(
        tmp_path,
        CliOverrides(provider="fake", runtime_engine=engine),
        data_paths=paths,
        environment={},
    )

    async def scenario() -> None:
        model = FakeModelPort(
            scripts=(
                (
                    ToolCallCompleted(ToolCall("tools", "search_tools", {"query": "echo"})),
                    ToolCallCompleted(
                        ToolCall("resources", "list_mcp_resources", {"kind": "resource_template"})
                    ),
                    ToolCallCompleted(
                        ToolCall("prompts", "search_mcp_prompts", {"query": "explain"})
                    ),
                    ResponseCompleted("tool_calls"),
                ),
                (
                    ToolCallCompleted(ToolCall("tool", "mcp__official__echo", {"value": "你好"})),
                    ToolCallCompleted(
                        ToolCall(
                            "resource",
                            "read_mcp_resource",
                            {"server_id": "official", "uri": "docs://example"},
                        )
                    ),
                    ToolCallCompleted(
                        ToolCall(
                            "prompt",
                            "get_mcp_prompt",
                            {
                                "server_id": "official",
                                "name": "explain",
                                "arguments": {"topic": "MCP"},
                            },
                        )
                    ),
                    ResponseCompleted("tool_calls"),
                ),
                (TextDelta("Used all capabilities"), ResponseCompleted("stop")),
                (TextDelta("Used direct prompt"), ResponseCompleted("stop")),
            )
        )
        runtime = build_runtime(
            config, FakeApproval(ApprovalDecision.APPROVE), model_wrapper=lambda _: model
        )
        try:
            turn = await runtime.coordinator.send("Discover and use all three MCP capabilities")
            assert turn.result.stop_reason.value == "completed" and turn.result.tool_calls == 6
            results = {
                event.payload.result.call_id: event.payload.result
                async for event in runtime.session.read(turn.session_id)
                if isinstance(event.payload, ToolExecutionCompletedPayload)
            }
            assert len(results) == 6
            assert all(result.error_code is None for result in results.values()), results
            for key, expected in (
                ("tool", "SDK tool: 你好"),
                ("resource", "SDK resource: example"),
                ("prompt", "SDK prompt: MCP"),
            ):
                outcome = results[key].outcome
                assert outcome is not None and expected in repr(outcome.data)
            direct = await runtime.coordinator.send(
                "/mcp prompt official explain topic=direct", session_id=turn.session_id
            )
            assert direct.result.stop_reason.value == "completed"
            material = await anext(
                event.payload
                async for event in runtime.session.read(turn.session_id)
                if isinstance(event.payload, MCPPromptPreparedPayload)
                and event.turn_id == direct.turn_id
            )
            assert "SDK prompt: direct" in repr(material.outcome.data)
        finally:
            await runtime.aclose()

    asyncio.run(scenario())
