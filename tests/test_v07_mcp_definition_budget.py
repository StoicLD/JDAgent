"""MCP schemas yield to the complete model input without truncating their constraints."""

import asyncio
import json
import sys
from pathlib import Path

from jdagent.adapters.fake import FakeApproval, FakeModelPort
from jdagent.composition import build_runtime
from jdagent.configuration import CliOverrides, resolve_configuration
from jdagent.data_paths import DataPaths
from jdagent.domain.model import ResponseCompleted, TextDelta, ToolCallCompleted
from jdagent.domain.tools import ApprovalDecision, ToolCall


def test_large_loaded_schema_yields_to_input_budget_before_history_compaction(
    tmp_path: Path,
) -> None:
    paths = DataPaths.for_workspace(
        tmp_path, config_root=tmp_path / "config", data_root=tmp_path / "data"
    )
    paths.user_config_file.parent.mkdir(parents=True)
    peer = Path(__file__).parent / "fixtures" / "mcp_stdio_server.py"
    paths.user_config_file.write_text(
        "model_context_window = 15000\nmax_context_tokens = 15000\nmax_output_tokens = 1000\n"
        "[memory]\nauto_consolidation = false\n"
        '[mcp.servers.docs]\nenabled = true\ntransport = "stdio"\n'
        f"command = {json.dumps(sys.executable)}\n"
        f"args = {json.dumps([str(peer), '2026-07-28', 'catalog_large_schema'])}\n"
        'tools = ["read"]\n',
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
                ToolCallCompleted(ToolCall("find", "search_tools", {"query": "read"})),
                ResponseCompleted("tool_calls"),
            ),
            (
                TextDelta("The complete definition does not fit this input budget."),
                ResponseCompleted("stop"),
            ),
        ),
        repeat_last=True,
    )

    async def scenario() -> None:
        runtime = build_runtime(
            config, FakeApproval(ApprovalDecision.APPROVE), model_wrapper=lambda _: model
        )
        try:
            turn = await runtime.coordinator.send("Find the remote reader")
            assert turn.result.stop_reason.value == "completed"
            assert len(model.requests) == 2
            assert all(item.metadata.get("call_kind") == "task" for item in model.requests)
            request = model.requests[-1]
            assert "mcp__docs__read" not in {tool.name for tool in request.tools}
            assert {"search_tools", "read_mcp_result"} <= {tool.name for tool in request.tools}
            assert json.loads(request.metadata["mcp_tools_omitted"]) == ["mcp__docs__read"]
            assert "p" * 100 not in json.dumps([tool.input_schema for tool in request.tools])
            await runtime.coordinator.send(
                "Continue without loading that tool", session_id=turn.session_id
            )
            assert len(model.requests) == 3
            assert "mcp__docs__read" not in {tool.name for tool in model.requests[-1].tools}
        finally:
            await runtime.aclose()

    asyncio.run(scenario())
