"""Prompt Toolkit terminal adapter for the interactive application seam."""

import json
from asyncio import CancelledError
from pathlib import Path

from prompt_toolkit import PromptSession
from prompt_toolkit.completion import WordCompleter
from prompt_toolkit.history import FileHistory
from prompt_toolkit.input import Input
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.key_binding.key_processor import KeyPressEvent
from prompt_toolkit.output import Output
from rich.console import Console, Group
from rich.live import Live
from rich.markdown import Markdown
from rich.text import Text

from jdagent.application.approval import ApprovalChoice, ApprovalScope
from jdagent.application.interactive import (
    CommandError,
    ExitAction,
    InteractionState,
    TerminalStatus,
    UiEvent,
    UiEventKind,
    UserAction,
    parse_user_action,
)
from jdagent.domain.events import (
    PermissionRequestedPayload,
    RuntimeEvent,
    RuntimeEventType,
    ToolCallRequestedPayload,
    ToolExecutionCompletedPayload,
)
from jdagent.domain.model import ModelEvent, TextDelta
from jdagent.domain.tools import ApprovalDecision, ApprovalRequest

_COMMANDS = (
    "/help",
    "/status",
    "/new",
    "/sessions",
    "/resume",
    "/rename",
    "/permissions",
    "/trace",
    "/exit",
)


def format_approval_request(request: ApprovalRequest) -> str:
    """Render only the pre-redacted approval summary supplied by the runtime."""

    parameters = json.dumps(request.arguments, ensure_ascii=False, sort_keys=True)
    return (
        f"Approval required: tool={request.tool_name} target={request.target or 'n/a'} "
        f"parameters={parameters} risk={request.risk.value} call={request.call_id}"
    )


def _key_bindings() -> KeyBindings:
    bindings = KeyBindings()

    @bindings.add("enter")
    def submit(event: KeyPressEvent) -> None:  # pyright: ignore[reportUnusedFunction]
        event.current_buffer.validate_and_handle()

    @bindings.add("escape", "enter")
    def newline(event: KeyPressEvent) -> None:  # pyright: ignore[reportUnusedFunction]
        event.current_buffer.insert_text("\n")

    return bindings


class PromptToolkitTerminal:
    """Read enhanced line-oriented input and return project-owned user actions."""

    def __init__(
        self,
        *,
        history_file: Path,
        input: Input | None = None,
        output: Output | None = None,
    ) -> None:
        history_file.parent.mkdir(parents=True, exist_ok=True)
        self._state = InteractionState.STARTING
        self._closed = False
        self._status = TerminalStatus(
            InteractionState.STARTING,
            "new",
            "unknown",
            "ask",
        )
        self._session: PromptSession[str] = PromptSession(
            history=FileHistory(str(history_file)),
            completer=WordCompleter(_COMMANDS, sentence=True),
            complete_while_typing=True,
            key_bindings=_key_bindings(),
            input=input,
            output=output,
        )

    async def next_action(self, state: InteractionState) -> UserAction:
        self._state = state
        while True:
            try:
                raw = await self._session.prompt_async(
                    "> ",
                    multiline=True,
                    bottom_toolbar=lambda: (
                        f" JDAgent | {self._status.session} | {self._status.model} | "
                        f"write={self._status.write_permission} | {self._status.state.value} "
                    ),
                )
            except EOFError:
                return ExitAction()
            except KeyboardInterrupt:
                return ExitAction(cancelled=True)
            try:
                return parse_user_action(raw)
            except CommandError as error:
                if str(error) == "Input must not be empty":
                    continue
                raise

    async def update_status(self, status: TerminalStatus) -> None:
        self._status = status

    async def request(self, request: ApprovalRequest) -> ApprovalChoice:
        try:
            answer = await self._session.prompt_async(
                f"{format_approval_request(request)}\n"
                "Allow [o]nce, session [f]ile, session [d]irectory? [N] ",
                multiline=False,
                bottom_toolbar=lambda: (
                    f" JDAgent | {self._status.session} | {self._status.model} | "
                    f"write={self._status.write_permission} | {self._status.state.value} "
                ),
            )
        except KeyboardInterrupt as error:
            raise CancelledError from error
        except EOFError:
            return ApprovalChoice(ApprovalDecision.REJECT)
        normalized = answer.strip().lower()
        if normalized in {"o", "once", "y", "yes"}:
            return ApprovalChoice(ApprovalDecision.APPROVE, ApprovalScope.ONCE)
        if normalized in {"f", "file"}:
            return ApprovalChoice(ApprovalDecision.APPROVE, ApprovalScope.SESSION_FILE)
        if normalized in {"d", "directory", "dir"}:
            return ApprovalChoice(ApprovalDecision.APPROVE, ApprovalScope.SESSION_DIRECTORY)
        return ApprovalChoice(ApprovalDecision.REJECT)

    async def close(self) -> None:
        self._closed = True


class RichPresenter:
    """Render semantic UI events and streaming model text with Rich."""

    def __init__(self, console: Console | None = None) -> None:
        self._console = console or Console()
        self._streamed_text = ""
        self._state_text = InteractionState.STARTING.value
        self._live: Live | None = None

    def _render_live(self) -> Group:
        body = Markdown(self._streamed_text) if self._streamed_text else Text("")
        return Group(body, Text(f"JDAgent | {self._state_text}", style="dim"))

    def _ensure_live(self) -> None:
        if self._live is None:
            self._live = Live(
                self._render_live(),
                console=self._console,
                refresh_per_second=12,
                transient=False,
            )
            self._live.start(refresh=True)
        else:
            self._live.update(self._render_live(), refresh=True)

    def _stop_live(self, *, reset_text: bool) -> None:
        if self._live is not None:
            self._live.stop()
            self._live = None
        if reset_text:
            self._streamed_text = ""

    async def observe(self, event: ModelEvent) -> None:
        if not isinstance(event, TextDelta):
            return
        self._streamed_text += event.text
        self._ensure_live()

    async def publish(self, event: UiEvent) -> None:
        if event.kind is UiEventKind.STATE:
            self._state_text = event.message
            if event.state is InteractionState.WAITING_APPROVAL:
                # Prompt Toolkit owns the persistent footer while it reads approval input.
                self._stop_live(reset_text=False)
            elif event.state in {
                InteractionState.RUNNING_TOOL,
                InteractionState.CANCELLING,
            }:
                self._ensure_live()
            elif self._live is not None:
                self._live.update(self._render_live(), refresh=True)
            return
        if event.kind is UiEventKind.ASSISTANT:
            if self._live is not None:
                self._streamed_text = event.message
                self._live.update(self._render_live(), refresh=True)
                self._stop_live(reset_text=True)
            else:
                self._console.print(Markdown(event.message))
            return
        if event.kind is UiEventKind.ERROR:
            self._stop_live(reset_text=True)
            self._console.print(f"Error: {event.message}", style="bold red")
        elif event.kind is UiEventKind.WARNING:
            self._stop_live(reset_text=True)
            self._console.print(f"Warning: {event.message}", style="yellow")
        else:
            self._console.print(event.message)

    async def close(self) -> None:
        self._stop_live(reset_text=True)


class RichRuntimeObserver:
    """Translate persisted tool lifecycle facts into semantic UI events."""

    def __init__(self, presenter: RichPresenter) -> None:
        self._presenter = presenter

    async def observe(self, event: RuntimeEvent) -> None:
        payload = event.payload
        if event.event_type is RuntimeEventType.TOOL_CALL_REQUESTED and isinstance(
            payload, ToolCallRequestedPayload
        ):
            await self._presenter.publish(
                UiEvent(UiEventKind.INFO, f"tool requested: {payload.call.name}")
            )
        elif event.event_type is RuntimeEventType.PERMISSION_REQUESTED and isinstance(
            payload, PermissionRequestedPayload
        ):
            target = payload.request.target or "n/a"
            await self._presenter.publish(
                UiEvent(
                    UiEventKind.INFO,
                    f"approval requested: {payload.request.tool_name} target={target}",
                )
            )
        elif event.event_type is RuntimeEventType.TOOL_EXECUTION_COMPLETED and isinstance(
            payload, ToolExecutionCompletedPayload
        ):
            await self._presenter.publish(
                UiEvent(
                    UiEventKind.INFO,
                    f"tool {payload.result.status.value}: {payload.result.tool_name}",
                )
            )
