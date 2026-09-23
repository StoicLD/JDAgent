import asyncio
import json
import runpy
import shutil
import sys
from collections.abc import AsyncIterator
from pathlib import Path
from typing import cast

from jdagent.adapters.fake import FakeApproval
from jdagent.adapters.jsonl_session import JsonlSession
from jdagent.domain.errors import StopReason
from jdagent.domain.events import ToolExecutionCompletedPayload
from jdagent.domain.json import JsonObject
from jdagent.domain.model import (
    ModelCapabilities,
    ModelEvent,
    ModelRequest,
    ResponseCompleted,
    TextDelta,
    ToolCallCompleted,
)
from jdagent.domain.tools import ApprovalDecision, ToolCall
from jdagent.tools.command_output import CommandLogs
from jdagent.tools.commands import create_command_tools
from jdagent.tools.files import create_file_tools
from tests.runtime_factory import create_test_coordinator

FIXTURE = Path(__file__).resolve().parents[1] / "testdata" / "v0.4-tools"


class ScriptedRepair:
    capabilities = ModelCapabilities()

    def __init__(self) -> None:
        self.step = 0

    async def stream(self, request: ModelRequest) -> AsyncIterator[ModelEvent]:
        args: JsonObject
        steps: list[tuple[str, JsonObject]] = [
            ("find_files", {}),
            ("search_text", {"query": "sum_prices"}),
            ("read_file", {"path": "prices.py"}),
            ("run_command", {"program": sys.executable, "args": ["verify.py"]}),
        ]
        if self.step < len(steps):
            name, args = steps[self.step]
        elif self.step == 4:
            read_message = next(
                message for message in request.messages if message.name == "read_file"
            )
            result = cast(dict[str, dict[str, str]], json.loads(read_message.content))
            name = "edit_file"
            args = {
                "path": "prices.py",
                "revision": result["data"]["revision"],
                "edits": [{"old_text": "total -= price", "new_text": "total += price"}],
            }
        elif self.step == 5:
            name = "run_command"
            args = {"program": sys.executable, "args": ["verify.py"]}
        else:
            yield TextDelta(
                "Initial verification failed; fixed subtraction and verification now passed."
            )
            yield ResponseCompleted("stop")
            return
        self.step += 1
        yield ToolCallCompleted(ToolCall(f"call-{self.step}", name, args))
        yield ResponseCompleted("tool_calls")


def test_scripted_model_repairs_real_files_and_runs_real_verification(tmp_path: Path) -> None:
    workspace = tmp_path / "project"
    shutil.copytree(FIXTURE, workspace)
    sentinel = (workspace / "verify.py").read_bytes()

    async def scenario() -> None:
        session = JsonlSession(tmp_path / "sessions")
        tools = (
            *create_file_tools(workspace),
            *create_command_tools(workspace, CommandLogs(tmp_path / "logs")),
        )
        coordinator = create_test_coordinator(
            model=ScriptedRepair(),
            session=session,
            tools=tools,
            approval=FakeApproval(ApprovalDecision.APPROVE),
            workspace=workspace,
        )
        turn = await coordinator.send("Find, repair and verify the price sum bug.")
        assert turn.result.stop_reason is StopReason.COMPLETED, turn.result
        results = [
            event.payload.result
            async for event in session.read(turn.session_id)
            if isinstance(event.payload, ToolExecutionCompletedPayload)
        ]
        assert {result.tool_name for result in results} == {
            "find_files",
            "search_text",
            "read_file",
            "edit_file",
            "run_command",
        }
        commands = [result for result in results if result.tool_name == "run_command"]
        assert commands[0].outcome is not None and commands[0].outcome.observed_failure
        assert commands[1].outcome is not None and commands[1].outcome.data["exit_code"] == 0
        assert (workspace / "verify.py").read_bytes() == sentinel
        namespace = runpy.run_path(str(workspace / "prices.py"))
        assert namespace["sum_prices"]([2, 3, 7]) == 12
        assert namespace["sum_prices"]([]) == 0
        assert namespace["sum_prices"]([-1, 4]) == 3

    asyncio.run(scenario())
