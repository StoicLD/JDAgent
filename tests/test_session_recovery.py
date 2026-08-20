import asyncio
import threading
from pathlib import Path

import pytest

import jdagent.adapters.jsonl_recovery as recovery_module
from jdagent.adapters.jsonl_recovery import JsonlRecoveryStore
from jdagent.adapters.jsonl_session import JsonlSession
from jdagent.adapters.memory import InMemorySession
from jdagent.application.session_recovery import (
    LogicalRecovery,
    PhysicalRecovery,
    SessionRecovery,
    classify_logical_recovery,
    close_interrupted_turn,
    create_recovery_session,
)
from jdagent.context import project_messages
from jdagent.domain.errors import StopReason
from jdagent.domain.events import (
    AssistantMessageCompletedPayload,
    RuntimeEvent,
    RuntimeEventType,
    SessionRenamedPayload,
    SessionStartedPayload,
    ToolExecutionStartedPayload,
    TurnCompletedPayload,
    TurnFailedPayload,
    UserMessagePayload,
)
from jdagent.domain.tools import RiskLevel
from jdagent.eventing import EventJournal


def test_recovery_backs_up_and_truncates_only_partial_tail(tmp_path: Path) -> None:
    session_directory = tmp_path / "sessions"

    async def scenario() -> tuple[bytes, bytes, PhysicalRecovery, int, tuple[str, ...]]:
        session = JsonlSession(session_directory)
        journal = await EventJournal.open(session, "session-1")
        await journal.record(
            None,
            RuntimeEventType.SESSION_STARTED,
            SessionStartedPayload(name="session-1", workspace_identity="workspace-1"),
        )
        session_path = session_directory / "session-1.jsonl"
        original_prefix = session_path.read_bytes()
        session_path.write_bytes(original_prefix + b'{"schema_version":1')

        result = await SessionRecovery(JsonlRecoveryStore(session_directory)).recover("session-1")
        strict_events = [event async for event in session.read("session-1")]
        assert result.backup_path is not None
        return (
            session_path.read_bytes(),
            result.backup_path.read_bytes(),
            result.physical,
            len(strict_events),
            await session.list_session_ids(),
        )

    repaired, backup, physical, event_count, identifiers = asyncio.run(scenario())

    assert repaired.endswith(b"\n")
    assert backup.endswith(b'{"schema_version":1')
    assert physical is PhysicalRecovery.REPAIRED_FINAL_PARTIAL_RECORD
    assert event_count == 1
    assert identifiers == ("session-1",)


def test_recovery_excludes_concurrent_session_append(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    directory = tmp_path / "sessions"
    entered = threading.Event()
    release = threading.Event()
    original_write = recovery_module._write_fsynced  # pyright: ignore[reportPrivateUsage]

    def blocking_write(path: Path, raw: bytes) -> None:
        if ".backup-" in path.name:
            entered.set()
            assert release.wait(timeout=2)
        original_write(path, raw)

    monkeypatch.setattr(recovery_module, "_write_fsynced", blocking_write)

    async def scenario() -> tuple[PhysicalRecovery, tuple[RuntimeEventType, ...]]:
        session = JsonlSession(directory)
        journal = await EventJournal.open(session, "session-1")
        await journal.record(
            None,
            RuntimeEventType.SESSION_STARTED,
            SessionStartedPayload("session-1", "workspace-1"),
        )
        path = session.path_for("session-1")
        path.write_bytes(path.read_bytes() + b'{"partial":')
        recovery_task = asyncio.create_task(
            SessionRecovery(JsonlRecoveryStore(directory)).recover("session-1")
        )
        assert await asyncio.to_thread(entered.wait, 2)
        append_task = asyncio.create_task(
            session.append(
                RuntimeEvent.create(
                    session_id="session-1",
                    turn_id=None,
                    sequence=2,
                    event_type=RuntimeEventType.SESSION_RENAMED,
                    payload=SessionRenamedPayload("renamed"),
                )
            )
        )
        await asyncio.sleep(0.02)
        assert not append_task.done()
        release.set()
        result = await recovery_task
        await append_task
        events = tuple([event async for event in session.read("session-1")])
        return result.physical, tuple(event.event_type for event in events)

    physical, event_types = asyncio.run(scenario())

    assert physical is PhysicalRecovery.REPAIRED_FINAL_PARTIAL_RECORD
    assert event_types == (
        RuntimeEventType.SESSION_STARTED,
        RuntimeEventType.SESSION_RENAMED,
    )


def test_recovery_rejects_corruption_before_final_line(tmp_path: Path) -> None:
    session_directory = tmp_path / "sessions"

    async def scenario() -> tuple[PhysicalRecovery, bytes]:
        session = JsonlSession(session_directory)
        journal = await EventJournal.open(session, "session-1")
        await journal.record(
            None,
            RuntimeEventType.SESSION_STARTED,
            SessionStartedPayload(name="session-1", workspace_identity="workspace-1"),
        )
        path = session_directory / "session-1.jsonl"
        raw = path.read_bytes()
        path.write_bytes(b"not-json\n" + raw)

        result = await SessionRecovery(JsonlRecoveryStore(session_directory)).recover("session-1")
        return result.physical, path.read_bytes()

    physical, unchanged = asyncio.run(scenario())

    assert physical is PhysicalRecovery.UNRECOVERABLE
    assert unchanged.startswith(b"not-json\n")


def test_started_write_is_uncertain_but_started_read_is_safely_interruptible() -> None:
    def events(risk: RiskLevel) -> tuple[RuntimeEvent, ...]:
        return (
            RuntimeEvent.create(
                session_id="session-1",
                turn_id=None,
                sequence=1,
                event_type=RuntimeEventType.SESSION_STARTED,
                payload=SessionStartedPayload(),
            ),
            RuntimeEvent.create(
                session_id="session-1",
                turn_id="turn-1",
                sequence=2,
                event_type=RuntimeEventType.USER_MESSAGE,
                payload=UserMessagePayload("question"),
            ),
            RuntimeEvent.create(
                session_id="session-1",
                turn_id="turn-1",
                sequence=3,
                event_type=RuntimeEventType.TOOL_EXECUTION_STARTED,
                payload=ToolExecutionStartedPayload("call-1", "tool", risk),
            ),
        )

    assert (
        classify_logical_recovery(events(RiskLevel.WRITE)) is LogicalRecovery.UNCERTAIN_SIDE_EFFECT
    )
    assert classify_logical_recovery(events(RiskLevel.READ)) is LogicalRecovery.INTERRUPTED_SAFE


def test_recovery_session_starts_at_last_safe_turn_and_preserves_original() -> None:
    async def scenario() -> tuple[int, tuple[str, ...], str]:
        session = InMemorySession()
        original = await EventJournal.open(session, "original")
        await original.record(
            None,
            RuntimeEventType.SESSION_STARTED,
            SessionStartedPayload(name="original", workspace_identity="workspace-1"),
        )
        await original.record(
            "turn-1", RuntimeEventType.USER_MESSAGE, UserMessagePayload("safe question")
        )
        await original.record(
            "turn-1",
            RuntimeEventType.ASSISTANT_MESSAGE_COMPLETED,
            AssistantMessageCompletedPayload("safe answer"),
        )
        await original.record(
            "turn-1",
            RuntimeEventType.TURN_COMPLETED,
            TurnCompletedPayload(StopReason.COMPLETED, 1, 0),
        )
        await original.record(
            "turn-2", RuntimeEventType.USER_MESSAGE, UserMessagePayload("unsafe question")
        )
        await original.record(
            "turn-2",
            RuntimeEventType.TOOL_EXECUTION_STARTED,
            ToolExecutionStartedPayload("call-1", "write_text_file", RiskLevel.WRITE),
        )
        recovered_id = await create_recovery_session(
            session=session,
            source_events=original.events,
            workspace_identity="workspace-1",
        )
        recovered_events = tuple([event async for event in session.read(recovered_id)])
        messages = tuple(message.content for message in project_messages(recovered_events))
        unchanged = len([event async for event in session.read("original")])
        return unchanged, messages, recovered_id

    original_count, messages, recovered_id = asyncio.run(scenario())

    assert original_count == 6
    assert messages == ("safe question", "safe answer")
    assert recovered_id != "original"


def test_incomplete_turn_before_side_effect_is_closed_as_interrupted() -> None:
    async def scenario() -> tuple[LogicalRecovery, RuntimeEventType, StopReason]:
        session = InMemorySession()
        journal = await EventJournal.open(session, "session-1")
        await journal.record(None, RuntimeEventType.SESSION_STARTED, SessionStartedPayload())
        await journal.record(
            "turn-1", RuntimeEventType.USER_MESSAGE, UserMessagePayload("question")
        )

        await close_interrupted_turn(session=session, events=journal.events)
        events = tuple([event async for event in session.read("session-1")])
        terminal = events[-1]
        assert isinstance(terminal.payload, TurnFailedPayload)
        return (
            classify_logical_recovery(events),
            terminal.event_type,
            terminal.payload.stop_reason,
        )

    logical, event_type, stop_reason = asyncio.run(scenario())

    assert logical is LogicalRecovery.CLEAN
    assert event_type is RuntimeEventType.TURN_FAILED
    assert stop_reason is StopReason.CANCELLED
