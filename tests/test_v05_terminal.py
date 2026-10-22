import asyncio
from pathlib import Path

from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput

from jdagent.adapters.fake import FakeApproval, FakeModelPort
from jdagent.adapters.memory import InMemorySession
from jdagent.adapters.scripted_terminal import CapturePresenter, ScriptedTerminal
from jdagent.adapters.terminal import PromptToolkitTerminal
from jdagent.application.interactive import (
    CommandAction,
    CommandName,
    ExitAction,
    InteractionState,
    InteractiveApplication,
    InteractiveContext,
    PromptAction,
)
from jdagent.application.session_catalog import SessionCatalog
from jdagent.domain.model import ResponseCompleted, TextDelta
from jdagent.domain.tools import ApprovalDecision, ApprovalRequest, RiskLevel
from tests.runtime_factory import create_test_coordinator


def test_control_command_during_approval_does_not_answer_the_approval(tmp_path: Path) -> None:
    async def scenario() -> None:
        with create_pipe_input() as pipe:
            terminal = PromptToolkitTerminal(
                history_file=tmp_path / "history", input=pipe, output=DummyOutput()
            )
            controls = asyncio.create_task(terminal.next_action(InteractionState.RUNNING_TURN))
            approval = asyncio.create_task(
                terminal.request(
                    ApprovalRequest(
                        "create_file", {}, RiskLevel.WRITE, "call", "session", "note.txt"
                    )
                )
            )
            try:
                await asyncio.sleep(0.01)
                pipe.send_text("/compact\n")
                action = await asyncio.wait_for(controls, 2)
                assert isinstance(action, CommandAction)
                assert action.name.value == "compact"
                assert not approval.done()
                pipe.send_text("o\n")
                choice = await asyncio.wait_for(approval, 2)
                assert choice.decision is ApprovalDecision.APPROVE
            finally:
                controls.cancel()
                approval.cancel()
                await asyncio.gather(controls, approval, return_exceptions=True)
                await terminal.close()

    asyncio.run(scenario())


def test_running_cli_queues_compaction_and_reports_window_without_extra_task_calls(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        session = InMemorySession()
        model = FakeModelPort(
            scripts=((TextDelta("done"), ResponseCompleted("stop")),), delay_seconds=0.05
        )
        coordinator = create_test_coordinator(
            model=model,
            session=session,
            tools=(),
            approval=FakeApproval(ApprovalDecision.APPROVE),
            workspace=tmp_path,
            workspace_identity="workspace",
        )
        terminal = ScriptedTerminal(
            (
                PromptAction("hello"),
                CommandAction(CommandName.COMPACT),
                CommandAction(CommandName.CONTEXT_WINDOW),
                ExitAction(),
            )
        )
        presenter = CapturePresenter()
        application = InteractiveApplication(
            terminal=terminal,
            presenter=presenter,
            coordinator=coordinator,
            session=session,
            catalog=SessionCatalog(
                session=session, discovery=session, workspace_identity="workspace"
            ),
            context=InteractiveContext("fake", "fake", tmp_path, "ask"),
        )
        assert await application.run() == 0
        assert len(model.requests) == 1
        assert any("queued" in event.message for event in presenter.events)
        assert any("input_budget=" in event.message for event in presenter.events)

    asyncio.run(scenario())
