"""Interactive CLI actions, command routing, and application lifecycle."""

from __future__ import annotations

import asyncio
import shlex
from asyncio import CancelledError
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Protocol, TypeAlias
from uuid import uuid4

from jdagent.application.coordinator import CoordinatedTurn, TurnCoordinator
from jdagent.application.headless import ExitStatus
from jdagent.application.permissions import revoke_session_rule
from jdagent.application.session_catalog import SessionCatalog
from jdagent.application.session_recovery import (
    SessionRecovery,
    prepare_session_resume,
)
from jdagent.core.loop import CancellationToken
from jdagent.domain.errors import SessionError, StopReason
from jdagent.domain.events import RuntimeEvent, RuntimeEventType
from jdagent.observability import TraceProjection
from jdagent.ports import SessionPort
from jdagent.tools.permissions import active_session_rules


class CommandName(StrEnum):
    """The complete v0.2 built-in command set."""

    HELP = "help"
    STATUS = "status"
    NEW = "new"
    SESSIONS = "sessions"
    RESUME = "resume"
    RENAME = "rename"
    PERMISSIONS = "permissions"
    TRACE = "trace"
    EXIT = "exit"


class CommandError(ValueError):
    """An unknown command or invalid command syntax."""


@dataclass(frozen=True, slots=True)
class PromptAction:
    text: str


@dataclass(frozen=True, slots=True)
class CommandAction:
    name: CommandName
    arguments: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class CancelAction:
    pass


@dataclass(frozen=True, slots=True)
class ExitAction:
    cancelled: bool = False


UserAction: TypeAlias = PromptAction | CommandAction | CancelAction | ExitAction


class InteractionState(StrEnum):
    STARTING = "starting"
    IDLE = "idle"
    RUNNING_TURN = "generating"
    WAITING_APPROVAL = "waiting_approval"
    RUNNING_TOOL = "running_tool"
    CANCELLING = "cancelling"
    EXITING = "exiting"


class UiEventKind(StrEnum):
    STATE = "state"
    ASSISTANT = "assistant"
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


@dataclass(frozen=True, slots=True)
class UiEvent:
    kind: UiEventKind
    message: str
    state: InteractionState | None = None


class TerminalPort(Protocol):
    async def next_action(self, state: InteractionState) -> UserAction: ...

    async def update_status(self, status: TerminalStatus) -> None: ...

    async def close(self) -> None: ...


class PresenterPort(Protocol):
    async def publish(self, event: UiEvent) -> None: ...


class InteractionRuntimeObserver:
    """Feed persisted runtime lifecycle facts back into the application state machine."""

    def __init__(self) -> None:
        self._application: InteractiveApplication | None = None

    def bind(self, application: InteractiveApplication) -> None:
        if self._application is not None:
            raise RuntimeError("Interaction runtime observer is already bound")
        self._application = application

    async def observe(self, event: RuntimeEvent) -> None:
        if self._application is not None:
            await self._application.observe_runtime_event(event)


@dataclass(frozen=True, slots=True)
class InteractiveContext:
    provider: str
    model: str
    workspace: Path
    write_permission: str
    model_timeout_seconds: float = 30.0
    tool_timeout_seconds: float = 10.0
    max_context_tokens: int | None = None


@dataclass(frozen=True, slots=True)
class TerminalStatus:
    state: InteractionState
    session: str
    model: str
    write_permission: str


def parse_user_action(raw: str) -> UserAction:
    """Parse one submitted line without sending command text to the model."""

    stripped = raw.strip()
    if not stripped:
        raise CommandError("Input must not be empty")
    if not stripped.startswith("/"):
        return PromptAction(raw)
    try:
        parts = shlex.split(stripped)
    except ValueError as error:
        raise CommandError(f"Invalid command syntax: {error}") from error
    command_text = parts[0][1:].lower()
    try:
        name = CommandName(command_text)
    except ValueError as error:
        raise CommandError(f"Unknown command: /{command_text}; use /help") from error
    return CommandAction(name, tuple(parts[1:]))


_HELP = (
    "/help /status /new /sessions /resume <name|id> /rename <name> "
    "/permissions [revoke <rule-id>] /trace /exit\n"
    "Enter submit | Alt+Enter newline | Ctrl+R history search | "
    "Ctrl+C cancels a running turn; at idle it exits"
)


class InteractiveApplication:
    """Own the REPL state and delegate facts to existing application seams."""

    def __init__(
        self,
        *,
        terminal: TerminalPort,
        presenter: PresenterPort,
        coordinator: TurnCoordinator,
        catalog: SessionCatalog,
        session: SessionPort,
        context: InteractiveContext,
        initial_session_id: str | None = None,
        recovery: SessionRecovery | None = None,
        workspace_identity: str | None = None,
    ) -> None:
        self._terminal = terminal
        self._presenter = presenter
        self._coordinator = coordinator
        self._catalog = catalog
        self._session = session
        self._context = context
        self._state = InteractionState.STARTING
        self._session_id = initial_session_id
        self._last_trace: TraceProjection | None = None
        self._recovery = recovery
        self._workspace_identity = workspace_identity
        self._initial_session_pending = initial_session_id is not None
        self._session_name = initial_session_id[:8] if initial_session_id else "new"
        self._active_turn_task: asyncio.Task[CoordinatedTurn] | None = None
        self._active_cancellation: CancellationToken | None = None

    @property
    def turn_running(self) -> bool:
        return self._active_turn_task is not None

    def cancel_current_turn(self) -> bool:
        """Cancel the active coordinator task without exiting the REPL."""

        task = self._active_turn_task
        cancellation = self._active_cancellation
        if task is None or cancellation is None or task.done():
            return False
        cancellation.cancel()
        task.cancel()
        return True

    async def observe_runtime_event(self, event: RuntimeEvent) -> None:
        """Project persisted tool and approval lifecycle facts into UI state."""

        if not self.turn_running:
            return
        if event.event_type is RuntimeEventType.PERMISSION_REQUESTED:
            await self._set_state(InteractionState.WAITING_APPROVAL)
        elif event.event_type is RuntimeEventType.TOOL_EXECUTION_STARTED:
            await self._set_state(InteractionState.RUNNING_TOOL)
        elif event.event_type in {
            RuntimeEventType.PERMISSION_RESOLVED,
            RuntimeEventType.TOOL_EXECUTION_COMPLETED,
        }:
            await self._set_state(InteractionState.RUNNING_TURN)

    async def _set_state(self, state: InteractionState) -> None:
        self._state = state
        await self._terminal.update_status(
            TerminalStatus(
                state=state,
                session=self._session_name,
                model=self._context.model,
                write_permission=self._context.write_permission,
            )
        )
        await self._presenter.publish(UiEvent(UiEventKind.STATE, state.value, state))

    async def run(self) -> int:
        """Run until an explicit exit action while always closing the terminal."""

        try:
            await self._set_state(InteractionState.STARTING)
            await self._presenter.publish(
                UiEvent(UiEventKind.INFO, "JDAgent interactive mode; use /help")
            )
            if self._initial_session_pending:
                assert self._session_id is not None
                self._session_id = await self._prepare_resume(self._session_id)
                self._last_trace = await self._trace_for_session(self._session_id)
                self._initial_session_pending = False
            await self._set_state(InteractionState.IDLE)
            while True:
                try:
                    action = await self._terminal.next_action(self._state)
                except CommandError as error:
                    await self._presenter.publish(UiEvent(UiEventKind.ERROR, str(error)))
                    continue
                if isinstance(action, ExitAction):
                    await self._set_state(InteractionState.EXITING)
                    return int(ExitStatus.CANCELLED if action.cancelled else ExitStatus.SUCCESS)
                if isinstance(action, CancelAction):
                    await self._set_state(InteractionState.EXITING)
                    return int(ExitStatus.CANCELLED)
                if isinstance(action, CommandAction):
                    should_exit = await self._handle_command(action)
                    if should_exit:
                        await self._set_state(InteractionState.EXITING)
                        return int(ExitStatus.SUCCESS)
                    await self._set_state(InteractionState.IDLE)
                    continue
                await self._run_prompt(action)
        finally:
            await self._terminal.close()

    async def _run_prompt(self, action: PromptAction) -> None:
        if not action.text.strip():
            return
        new_session_id: str | None = None
        if self._session_id is None:
            new_session_id = str(uuid4())
            self._session_id = new_session_id
            self._session_name = f"session-{new_session_id[:8]}"
        await self._set_state(InteractionState.RUNNING_TURN)
        cancellation = CancellationToken()
        turn_task = asyncio.create_task(
            self._coordinator.send(
                action.text,
                session_id=None if new_session_id is not None else self._session_id,
                new_session_id=new_session_id,
                cancellation=cancellation,
            )
        )
        self._active_cancellation = cancellation
        self._active_turn_task = turn_task
        try:
            turn = await turn_task
            self._session_id = turn.session_id
            self._last_trace = turn.trace
            if turn.result.assistant_text:
                await self._presenter.publish(
                    UiEvent(UiEventKind.ASSISTANT, turn.result.assistant_text)
                )
            if turn.result.stop_reason is not StopReason.COMPLETED:
                await self._presenter.publish(
                    UiEvent(
                        UiEventKind.ERROR,
                        f"turn stopped: {turn.result.stop_reason.value}",
                    )
                )
        except CancelledError:
            await self._set_state(InteractionState.CANCELLING)
            await self._presenter.publish(
                UiEvent(
                    UiEventKind.WARNING,
                    "Current turn cancelled; session remains available",
                )
            )
        except (OSError, SessionError, ValueError) as error:
            await self._presenter.publish(UiEvent(UiEventKind.ERROR, str(error)))
        finally:
            self._active_turn_task = None
            self._active_cancellation = None
            await self._set_state(InteractionState.IDLE)

    async def _handle_command(self, action: CommandAction) -> bool:
        try:
            if action.name is CommandName.EXIT:
                self._require_arity(action.arguments, 0, "/exit")
                return True
            if action.name is CommandName.HELP:
                self._require_arity(action.arguments, 0, "/help")
                await self._presenter.publish(UiEvent(UiEventKind.INFO, _HELP))
            elif action.name is CommandName.STATUS:
                self._require_arity(action.arguments, 0, "/status")
                summary = self._last_trace.summary if self._last_trace is not None else None
                last_stop = (
                    summary.stop_reason.value
                    if summary is not None and summary.stop_reason is not None
                    else "none"
                )
                await self._presenter.publish(
                    UiEvent(
                        UiEventKind.INFO,
                        f"provider={self._context.provider} model={self._context.model} "
                        f"session={self._session_name} workspace={self._context.workspace} "
                        f"write_permission={self._context.write_permission} "
                        f"model_timeout={self._context.model_timeout_seconds:g}s "
                        f"tool_timeout={self._context.tool_timeout_seconds:g}s "
                        f"max_context={self._context.max_context_tokens or 'provider-default'} "
                        f"model_calls={summary.model_calls if summary else 0} "
                        f"tool_calls={summary.tool_calls if summary else 0} "
                        f"last_stop={last_stop}",
                    )
                )
            elif action.name is CommandName.NEW:
                self._require_arity(action.arguments, 0, "/new")
                self._session_id = None
                self._session_name = "new"
                self._last_trace = None
                await self._presenter.publish(UiEvent(UiEventKind.INFO, "new session ready"))
            elif action.name is CommandName.SESSIONS:
                self._require_arity(action.arguments, 0, "/sessions")
                sessions = await self._catalog.list_sessions()
                message = (
                    "\n".join(
                        f"{item.name} {item.short_id} {item.updated_at.isoformat()} "
                        f"workspace={self._context.workspace} {item.status}"
                        for item in sessions
                    )
                    or "no sessions"
                )
                await self._presenter.publish(UiEvent(UiEventKind.INFO, message))
            elif action.name is CommandName.RESUME:
                self._require_arity(action.arguments, 1, "/resume <name|id>")
                selected = await self._catalog.resolve(action.arguments[0])
                self._session_id = await self._prepare_resume(selected.session_id)
                self._last_trace = await self._trace_for_session(self._session_id)
                display_name = (
                    selected.name
                    if self._session_id == selected.session_id
                    else f"recovery-{selected.session_id[:8]}"
                )
                self._session_name = display_name
                await self._presenter.publish(
                    UiEvent(
                        UiEventKind.INFO,
                        f"resumed {display_name} {self._session_id[:8]}",
                    )
                )
            elif action.name is CommandName.RENAME:
                if not action.arguments:
                    raise CommandError("usage: /rename <name>")
                session_id = self._require_session()
                renamed = await self._catalog.rename(session_id, " ".join(action.arguments))
                self._session_name = renamed.name
                await self._presenter.publish(
                    UiEvent(UiEventKind.INFO, f"renamed session to {renamed.name}")
                )
            elif action.name is CommandName.PERMISSIONS:
                await self._permissions(action.arguments)
            elif action.name is CommandName.TRACE:
                self._require_arity(action.arguments, 0, "/trace")
                await self._trace()
        except (CommandError, OSError, SessionError, ValueError) as error:
            await self._presenter.publish(UiEvent(UiEventKind.ERROR, str(error)))
        return False

    async def _prepare_resume(self, session_id: str) -> str:
        if self._recovery is None:
            return session_id
        if self._workspace_identity is None:
            raise ValueError("workspace identity is required for recovery")
        prepared = await prepare_session_resume(
            session=self._session,
            recovery=self._recovery,
            session_id=session_id,
            workspace_identity=self._workspace_identity,
            allow_recovery_snapshot=True,
        )
        for warning in prepared.warnings:
            await self._presenter.publish(UiEvent(UiEventKind.WARNING, warning))
        return prepared.session_id

    async def _permissions(self, arguments: Sequence[str]) -> None:
        session_id = self._require_session()
        events = tuple([event async for event in self._session.read(session_id)])
        if not arguments:
            rules = active_session_rules(events)
            message = (
                "\n".join(
                    f"{rule.rule_id} {rule.tool_name} {rule.target_kind.value}:{rule.target}"
                    for rule in rules
                )
                or "no session permission rules"
            )
            await self._presenter.publish(UiEvent(UiEventKind.INFO, message))
            return
        if len(arguments) == 2 and arguments[0] == "revoke":
            await revoke_session_rule(self._session, session_id, arguments[1])
            await self._presenter.publish(
                UiEvent(UiEventKind.INFO, f"revoked permission rule {arguments[1]}")
            )
            return
        raise CommandError("usage: /permissions [revoke <rule-id>]")

    async def _trace(self) -> None:
        if self._last_trace is None:
            raise CommandError("No completed turn trace is available")
        summary = self._last_trace.summary
        await self._presenter.publish(
            UiEvent(
                UiEventKind.INFO,
                f"events={summary.event_count} model_calls={summary.model_calls} "
                f"tool_calls={summary.tool_calls} stop_reason="
                f"{summary.stop_reason.value if summary.stop_reason else 'none'}",
            )
        )

    async def _trace_for_session(self, session_id: str) -> TraceProjection:
        trace = TraceProjection()
        async for event in self._session.read(session_id):
            await trace.observe(event)
        return trace

    def _require_session(self) -> str:
        if self._session_id is None:
            raise CommandError("No current session; send a prompt or use /resume")
        return self._session_id

    @staticmethod
    def _require_arity(arguments: Sequence[str], expected: int, usage: str) -> None:
        if len(arguments) != expected:
            raise CommandError(f"usage: {usage}")
