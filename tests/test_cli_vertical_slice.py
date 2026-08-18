import asyncio
from pathlib import Path

from jdagent.adapters.fake import FakeApproval, FakeModelPort
from jdagent.adapters.memory import InMemorySession
from jdagent.cli import run_single_turn
from jdagent.domain.events import RuntimeEventType
from jdagent.domain.model import ResponseCompleted, TextDelta, ToolCallCompleted
from jdagent.domain.tools import ApprovalDecision, ToolCall
from jdagent.tools.builtins import create_builtin_tools
from jdagent.tools.workspace import WorkspacePathResolver
from tests.runtime_factory import create_test_coordinator


def test_cli_fake_vertical_slice_completes_tool_round_trip(tmp_path: Path) -> None:
    call = ToolCall("call-1", "calculator", {"expression": "2 + 3"})
    model = FakeModelPort(
        scripts=(
            (ToolCallCompleted(call), ResponseCompleted("tool_calls")),
            (TextDelta("The answer is 5."), ResponseCompleted("stop")),
        )
    )
    coordinator = create_test_coordinator(
        model=model,
        session=InMemorySession(),
        tools=create_builtin_tools(WorkspacePathResolver(tmp_path)),
        approval=FakeApproval(ApprovalDecision.APPROVE),
        workspace=tmp_path,
    )
    output: list[str] = []

    turn = asyncio.run(
        run_single_turn(coordinator, "calculate 2 + 3", output_function=output.append)
    )

    assert output[0] == "The answer is 5."
    assert output[-1] == "stop_reason=completed"
    assert [entry.event_type for entry in turn.trace.entries][-1] is RuntimeEventType.TURN_COMPLETED
    tool_entry = next(
        entry
        for entry in turn.trace.entries
        if entry.event_type is RuntimeEventType.TOOL_EXECUTION_COMPLETED
    )
    assert tool_entry.tool_status == "success"


def test_cli_rejected_write_has_no_side_effect(tmp_path: Path) -> None:
    call = ToolCall(
        "call-1",
        "write_text_file",
        {"path": "note.txt", "content": "do not write"},
    )
    model = FakeModelPort(
        scripts=(
            (ToolCallCompleted(call), ResponseCompleted("tool_calls")),
            (TextDelta("Write was rejected."), ResponseCompleted("stop")),
        )
    )
    session = InMemorySession()
    coordinator = create_test_coordinator(
        model=model,
        session=session,
        tools=create_builtin_tools(WorkspacePathResolver(tmp_path)),
        approval=FakeApproval(ApprovalDecision.REJECT),
        workspace=tmp_path,
    )

    turn = asyncio.run(coordinator.send("write a note"))

    assert not (tmp_path / "note.txt").exists()
    assert RuntimeEventType.PERMISSION_REQUESTED in {
        entry.event_type for entry in turn.trace.entries
    }
    assert RuntimeEventType.PERMISSION_RESOLVED in {
        entry.event_type for entry in turn.trace.entries
    }
    resolved = next(
        entry
        for entry in turn.trace.entries
        if entry.event_type is RuntimeEventType.PERMISSION_RESOLVED
    )
    assert resolved.permission_policy == "ask"
    assert resolved.approval_decision == "reject"
