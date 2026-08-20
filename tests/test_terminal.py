import asyncio
from io import StringIO
from pathlib import Path

from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput
from rich.console import Console

from jdagent.adapters.terminal import PromptToolkitTerminal, RichPresenter
from jdagent.application.approval import ApprovalChoice, ApprovalScope
from jdagent.application.interactive import (
    CommandAction,
    CommandName,
    ExitAction,
    InteractionState,
    UiEvent,
    UiEventKind,
)
from jdagent.domain.model import TextDelta
from jdagent.domain.tools import ApprovalDecision, ApprovalRequest, RiskLevel


def test_prompt_toolkit_terminal_returns_typed_command(tmp_path: Path) -> None:
    async def scenario() -> CommandAction:
        with create_pipe_input() as pipe_input:
            terminal = PromptToolkitTerminal(
                history_file=tmp_path / "history",
                input=pipe_input,
                output=DummyOutput(),
            )
            pending = asyncio.create_task(terminal.next_action(InteractionState.IDLE))
            await asyncio.sleep(0)
            pipe_input.send_text("/help\n")
            action = await pending
            await terminal.close()
            assert isinstance(action, CommandAction)
            return action

    assert asyncio.run(scenario()) == CommandAction(CommandName.HELP)


def test_ctrl_c_while_idle_returns_cancelled_exit(tmp_path: Path) -> None:
    async def scenario() -> ExitAction:
        with create_pipe_input() as pipe_input:
            terminal = PromptToolkitTerminal(
                history_file=tmp_path / "history",
                input=pipe_input,
                output=DummyOutput(),
            )
            pending = asyncio.create_task(terminal.next_action(InteractionState.IDLE))
            await asyncio.sleep(0)
            pipe_input.send_bytes(b"\x03")
            action = await pending
            await terminal.close()
            assert isinstance(action, ExitAction)
            return action

    assert asyncio.run(scenario()) == ExitAction(cancelled=True)


def test_prompt_toolkit_approval_returns_typed_scope(tmp_path: Path) -> None:
    async def scenario() -> ApprovalChoice:
        with create_pipe_input() as pipe_input:
            terminal = PromptToolkitTerminal(
                history_file=tmp_path / "history",
                input=pipe_input,
                output=DummyOutput(),
            )
            pending = asyncio.create_task(
                terminal.request(
                    ApprovalRequest(
                        "write_text_file",
                        {"content": "<7 chars>", "path": "note.txt"},
                        RiskLevel.WRITE,
                        "call-1",
                        "session-1",
                        "note.txt",
                    )
                )
            )
            await asyncio.sleep(0)
            pipe_input.send_text("f\n")
            choice = await pending
            await terminal.close()
            return choice

    assert asyncio.run(scenario()) == ApprovalChoice(
        ApprovalDecision.APPROVE,
        ApprovalScope.SESSION_FILE,
    )


def test_rich_presenter_renders_semantic_events_without_ansi_when_disabled() -> None:
    output = StringIO()
    presenter = RichPresenter(
        Console(file=output, force_terminal=False, color_system=None, width=80)
    )

    async def scenario() -> None:
        await presenter.publish(
            UiEvent(UiEventKind.STATE, "generating", InteractionState.RUNNING_TURN)
        )
        await presenter.observe(TextDelta("**streamed** answer"))
        await presenter.publish(
            UiEvent(UiEventKind.STATE, "running_tool", InteractionState.RUNNING_TOOL)
        )
        await presenter.publish(UiEvent(UiEventKind.INFO, "tool running: read_text_file"))
        await presenter.close()
        assert "running_tool" in output.getvalue()
        await presenter.publish(
            UiEvent(UiEventKind.STATE, "generating", InteractionState.RUNNING_TURN)
        )
        await presenter.observe(TextDelta("**streamed** answer"))
        await presenter.publish(UiEvent(UiEventKind.ASSISTANT, "**streamed** answer"))
        await presenter.publish(UiEvent(UiEventKind.ERROR, "safe error"))
        await presenter.close()

    asyncio.run(scenario())
    rendered = output.getvalue()

    assert "streamed" in rendered
    assert "tool running: read_text_file" in rendered
    assert "safe error" in rendered
    assert "\x1b[" not in rendered
