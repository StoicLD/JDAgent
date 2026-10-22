import asyncio
from pathlib import Path

from jdagent.adapters.fake import FakeApproval, FakeModelPort
from jdagent.adapters.memory import InMemorySession
from jdagent.domain.events import ToolExecutionCompletedPayload
from jdagent.domain.model import ResponseCompleted, TextDelta, ToolCallCompleted
from jdagent.domain.tools import ApprovalDecision, ToolCall
from jdagent.tools.files import create_file_tools
from tests.runtime_factory import create_test_coordinator


def test_new_directory_rules_stop_writes_until_the_model_has_seen_them(tmp_path: Path) -> None:
    (tmp_path / "nested").mkdir()
    (tmp_path / "nested" / "AGENTS.md").write_text("Use UTF-8 in this directory.", encoding="utf-8")

    async def scenario() -> None:
        session = InMemorySession()
        model = FakeModelPort(
            scripts=(
                (
                    ToolCallCompleted(
                        ToolCall("first", "create_file", {"path": "nested/a.txt", "content": "a"})
                    ),
                    ToolCallCompleted(
                        ToolCall("second", "create_file", {"path": "nested/b.txt", "content": "b"})
                    ),
                    ResponseCompleted("tool_calls"),
                ),
                (
                    TextDelta("I have read the new rules and will replan."),
                    ResponseCompleted("stop"),
                ),
            )
        )
        coordinator = create_test_coordinator(
            model=model,
            session=session,
            tools=create_file_tools(tmp_path),
            approval=FakeApproval(ApprovalDecision.APPROVE),
            workspace=tmp_path,
        )
        turn = await coordinator.send("Create two files in nested.")
        assert not (tmp_path / "nested/a.txt").exists()
        assert not (tmp_path / "nested/b.txt").exists()
        assert any("Use UTF-8" in part.content for part in model.requests[-1].system_parts)
        results = [
            event.payload.result
            for event in [event async for event in session.read(turn.session_id)]
            if isinstance(event.payload, ToolExecutionCompletedPayload)
        ]
        assert [result.error_code.value if result.error_code else None for result in results] == [
            "instructions_discovered",
            "not_executed",
        ]
        assert all(
            result.outcome is not None and not result.outcome.observed_failure for result in results
        )

    asyncio.run(scenario())
