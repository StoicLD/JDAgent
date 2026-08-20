import asyncio
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from jdagent.adapters.fake import FakeApproval, FakeModelPort
from jdagent.adapters.memory import InMemorySession
from jdagent.adapters.scripted_terminal import CapturePresenter, ScriptedTerminal
from jdagent.application.interactive import (
    CommandAction,
    CommandName,
    ExitAction,
    InteractionRuntimeObserver,
    InteractionState,
    InteractiveApplication,
    InteractiveContext,
    PromptAction,
    UiEventKind,
    parse_user_action,
)
from jdagent.application.session_catalog import SessionCatalog
from jdagent.application.session_recovery import (
    PhysicalRecovery,
    PhysicalRecoveryResult,
    SessionRecovery,
)
from jdagent.domain.errors import StopReason
from jdagent.domain.events import (
    RuntimeEvent,
    RuntimeEventType,
    SessionStartedPayload,
    ToolExecutionStartedPayload,
    TurnCompletedPayload,
    TurnFailedPayload,
    UserMessagePayload,
)
from jdagent.domain.model import (
    ModelEvent,
    ModelRequest,
    ResponseCompleted,
    TextDelta,
    ToolCallCompleted,
)
from jdagent.domain.tools import ApprovalDecision, RiskLevel, ToolCall
from jdagent.eventing import EventJournal
from jdagent.tools.builtins import create_builtin_tools
from jdagent.tools.workspace import WorkspacePathResolver
from tests.runtime_factory import create_test_coordinator


class CancelOnceFakeModel(FakeModelPort):
    """Hold only the first request until its owning task is cancelled."""

    def __init__(self) -> None:
        super().__init__(
            scripts=((TextDelta("answer"), ResponseCompleted("stop")),),
            repeat_last=True,
        )
        self.started = asyncio.Event()

    async def stream(self, request: ModelRequest) -> AsyncIterator[ModelEvent]:
        if not self.started.is_set():
            self.requests.append(request)
            self.started.set()
            await asyncio.Event().wait()
            return
        async for event in super().stream(request):
            yield event


class MemoryRecoveryStore:
    def __init__(self, session: InMemorySession) -> None:
        self._session = session

    async def recover_physical(self, session_id: str) -> PhysicalRecoveryResult:
        events = tuple([event async for event in self._session.read(session_id)])
        return PhysicalRecoveryResult(PhysicalRecovery.NO_REPAIR_NEEDED, events)


@pytest.mark.parametrize(
    ("raw", "name", "arguments"),
    (
        ("/help", CommandName.HELP, ()),
        ("/status", CommandName.STATUS, ()),
        ("/new", CommandName.NEW, ()),
        ("/sessions", CommandName.SESSIONS, ()),
        ("/resume work", CommandName.RESUME, ("work",)),
        ("/rename focused work", CommandName.RENAME, ("focused", "work")),
        ("/permissions", CommandName.PERMISSIONS, ()),
        ("/trace", CommandName.TRACE, ()),
        ("/exit", CommandName.EXIT, ()),
    ),
)
def test_command_router_parses_all_builtin_commands(
    raw: str,
    name: CommandName,
    arguments: tuple[str, ...],
) -> None:
    action = parse_user_action(raw)

    assert action == CommandAction(name, arguments)


def test_repl_routes_commands_without_sending_them_to_model(tmp_path: Path) -> None:
    session = InMemorySession()
    model = FakeModelPort(scripts=((TextDelta("answer"), ResponseCompleted("stop")),))
    coordinator = create_test_coordinator(
        model=model,
        session=session,
        tools=(),
        approval=FakeApproval(ApprovalDecision.APPROVE),
        workspace=tmp_path,
        workspace_identity="workspace-1",
    )
    terminal = ScriptedTerminal(
        (
            PromptAction("hello"),
            CommandAction(CommandName.STATUS),
            ExitAction(),
        )
    )
    presenter = CapturePresenter()
    application = InteractiveApplication(
        terminal=terminal,
        presenter=presenter,
        coordinator=coordinator,
        catalog=SessionCatalog(
            session=session,
            discovery=session,
            workspace_identity="workspace-1",
        ),
        session=session,
        context=InteractiveContext(
            provider="fake",
            model="fake",
            workspace=tmp_path,
            write_permission="ask",
        ),
    )

    code = asyncio.run(application.run())

    assert code == 0
    assert len(model.requests) == 1
    assert [message.content for message in model.requests[0].messages] == ["hello"]
    assert terminal.closed is True
    assert any(
        event.kind is UiEventKind.ASSISTANT and event.message == "answer"
        for event in presenter.events
    )
    assert any(
        event.kind is UiEventKind.INFO and "provider=fake" in event.message
        for event in presenter.events
    )


def test_repl_dispatches_all_session_commands_and_exit_keeps_session_resumable(
    tmp_path: Path,
) -> None:
    async def scenario() -> tuple[int, int, tuple[str, ...], bool]:
        session = InMemorySession()
        model = FakeModelPort(scripts=((TextDelta("answer"), ResponseCompleted("stop")),))
        coordinator = create_test_coordinator(
            model=model,
            session=session,
            tools=(),
            approval=FakeApproval(ApprovalDecision.APPROVE),
            workspace=tmp_path,
            workspace_identity="workspace-1",
        )
        catalog = SessionCatalog(
            session=session,
            discovery=session,
            workspace_identity="workspace-1",
        )
        terminal = ScriptedTerminal(
            (
                PromptAction("hello"),
                CommandAction(CommandName.RENAME, ("work",)),
                CommandAction(CommandName.SESSIONS),
                CommandAction(CommandName.PERMISSIONS),
                CommandAction(CommandName.TRACE),
                CommandAction(CommandName.NEW),
                CommandAction(CommandName.RESUME, ("work",)),
                CommandAction(CommandName.STATUS),
                CommandAction(CommandName.HELP),
                CommandAction(CommandName.EXIT),
            )
        )
        presenter = CapturePresenter()
        application = InteractiveApplication(
            terminal=terminal,
            presenter=presenter,
            coordinator=coordinator,
            catalog=catalog,
            session=session,
            context=InteractiveContext("fake", "fake", tmp_path, "ask"),
        )

        code = await application.run()
        resolved = await catalog.resolve("work")
        return (
            code,
            len(model.requests),
            tuple(event.message for event in presenter.events),
            bool(resolved.session_id),
        )

    code, request_count, messages, resumable = asyncio.run(scenario())

    assert code == 0
    assert request_count == 1
    assert resumable is True
    assert any("renamed session to work" in message for message in messages)
    assert any("no session permission rules" in message for message in messages)
    assert any("events=" in message for message in messages)
    assert any("resumed work" in message for message in messages)
    assert any("/permissions" in message for message in messages)


def test_cancelled_new_turn_returns_to_prompt_and_keeps_same_session(
    tmp_path: Path,
) -> None:
    async def scenario() -> tuple[int, tuple[RuntimeEvent, ...], int]:
        session = InMemorySession()
        model = CancelOnceFakeModel()
        coordinator = create_test_coordinator(
            model=model,
            session=session,
            tools=(),
            approval=FakeApproval(ApprovalDecision.APPROVE),
            workspace=tmp_path,
            workspace_identity="workspace-1",
        )
        application = InteractiveApplication(
            terminal=ScriptedTerminal(
                (PromptAction("cancel me"), PromptAction("continue"), ExitAction())
            ),
            presenter=CapturePresenter(),
            coordinator=coordinator,
            catalog=SessionCatalog(
                session=session,
                discovery=session,
                workspace_identity="workspace-1",
            ),
            session=session,
            context=InteractiveContext("fake", "fake", tmp_path, "ask"),
        )

        task = asyncio.create_task(application.run())
        await model.started.wait()
        assert application.cancel_current_turn() is True
        code = await task
        identifiers = await session.list_session_ids()
        events = tuple([event async for event in session.read(identifiers[0])])
        return code, events, len(model.requests)

    code, events, request_count = asyncio.run(scenario())

    cancelled = [
        event
        for event in events
        if event.event_type is RuntimeEventType.TURN_FAILED
        and isinstance(event.payload, TurnFailedPayload)
        and event.payload.stop_reason is StopReason.CANCELLED
    ]
    assert code == 0
    assert len(cancelled) == 1
    assert request_count == 2
    assert len({event.session_id for event in events}) == 1


def test_runtime_events_drive_waiting_approval_and_running_tool_states(
    tmp_path: Path,
) -> None:
    session = InMemorySession()
    observer = InteractionRuntimeObserver()
    model = FakeModelPort(
        scripts=(
            (
                ToolCallCompleted(
                    ToolCall(
                        "call-1",
                        "write_text_file",
                        {"path": "note.txt", "content": "created"},
                    )
                ),
                ResponseCompleted("tool_calls"),
            ),
            (TextDelta("done"), ResponseCompleted("stop")),
        )
    )
    coordinator = create_test_coordinator(
        model=model,
        session=session,
        tools=create_builtin_tools(WorkspacePathResolver(tmp_path)),
        approval=FakeApproval(ApprovalDecision.APPROVE),
        workspace=tmp_path,
        workspace_identity="workspace-1",
        event_observers=(observer,),
    )
    terminal = ScriptedTerminal((PromptAction("write"), ExitAction()))
    application = InteractiveApplication(
        terminal=terminal,
        presenter=CapturePresenter(),
        coordinator=coordinator,
        catalog=SessionCatalog(
            session=session,
            discovery=session,
            workspace_identity="workspace-1",
        ),
        session=session,
        context=InteractiveContext("fake", "fake", tmp_path, "ask"),
    )
    observer.bind(application)

    code = asyncio.run(application.run())

    states = tuple(status.state for status in terminal.statuses)
    assert code == 0
    assert InteractionState.WAITING_APPROVAL in states
    assert InteractionState.RUNNING_TOOL in states
    assert (tmp_path / "note.txt").read_text(encoding="utf-8") == "created"


def test_resume_of_uncertain_write_creates_safe_recovery_session(
    tmp_path: Path,
) -> None:
    async def scenario() -> tuple[tuple[str, ...], int, tuple[str, ...]]:
        session = InMemorySession()
        journal = await EventJournal.open(session, "original")
        await journal.record(
            None,
            RuntimeEventType.SESSION_STARTED,
            SessionStartedPayload("original", "workspace-1"),
        )
        await journal.record("turn-1", RuntimeEventType.USER_MESSAGE, UserMessagePayload("safe"))
        await journal.record(
            "turn-1",
            RuntimeEventType.TURN_COMPLETED,
            TurnCompletedPayload(StopReason.COMPLETED, 1, 0),
        )
        await journal.record("turn-2", RuntimeEventType.USER_MESSAGE, UserMessagePayload("unsafe"))
        await journal.record(
            "turn-2",
            RuntimeEventType.TOOL_EXECUTION_STARTED,
            ToolExecutionStartedPayload("call-1", "write_text_file", RiskLevel.WRITE),
        )
        presenter = CapturePresenter()
        application = InteractiveApplication(
            terminal=ScriptedTerminal(
                (CommandAction(CommandName.RESUME, ("original",)), ExitAction())
            ),
            presenter=presenter,
            coordinator=create_test_coordinator(
                model=FakeModelPort(scripts=((TextDelta("unused"), ResponseCompleted("stop")),)),
                session=session,
                tools=(),
                approval=FakeApproval(ApprovalDecision.APPROVE),
                workspace=tmp_path,
                workspace_identity="workspace-1",
            ),
            catalog=SessionCatalog(
                session=session,
                discovery=session,
                workspace_identity="workspace-1",
            ),
            session=session,
            context=InteractiveContext("fake", "fake", tmp_path, "ask"),
            recovery=SessionRecovery(MemoryRecoveryStore(session)),
            workspace_identity="workspace-1",
        )

        await application.run()
        identifiers = await session.list_session_ids()
        original_events = tuple([event async for event in session.read("original")])
        return (
            identifiers,
            len(original_events),
            tuple(event.message for event in presenter.events),
        )

    identifiers, original_count_after, messages = asyncio.run(scenario())

    assert len(identifiers) == 2
    assert original_count_after == 5
    assert any("side effect is uncertain" in message for message in messages)
    assert any("recovery-" in message for message in messages)
