"""Classify physical and logical session recovery outcomes."""

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Protocol
from uuid import uuid4

from jdagent.context import project_messages
from jdagent.domain.errors import SessionError, SessionErrorCode, StopReason
from jdagent.domain.events import (
    AssistantMessageCompletedPayload,
    RecoverySnapshotPayload,
    RuntimeEvent,
    RuntimeEventType,
    SessionStartedPayload,
    ToolExecutionCompletedPayload,
    ToolExecutionStartedPayload,
    TurnFailedPayload,
)
from jdagent.domain.tools import RiskLevel
from jdagent.eventing import EventJournal
from jdagent.ports import SessionPort


class PhysicalRecovery(StrEnum):
    """Whether storage needed a provably safe physical repair."""

    NO_REPAIR_NEEDED = "no_repair_needed"
    REPAIRED_FINAL_PARTIAL_RECORD = "repaired_final_partial_record"
    UNRECOVERABLE = "unrecoverable"


class LogicalRecovery(StrEnum):
    """Whether the durable event prefix can safely continue."""

    CLEAN = "clean"
    INTERRUPTED_SAFE = "interrupted_safe"
    UNCERTAIN_SIDE_EFFECT = "uncertain_side_effect"


@dataclass(frozen=True, slots=True)
class PhysicalRecoveryResult:
    physical: PhysicalRecovery
    events: tuple[RuntimeEvent, ...]
    backup_path: Path | None = None
    byte_offset: int | None = None
    message: str | None = None


class RecoveryStorePort(Protocol):
    """Validate and optionally repair only a storage-level final partial record."""

    async def recover_physical(self, session_id: str) -> PhysicalRecoveryResult: ...


@dataclass(frozen=True, slots=True)
class RecoveryResult:
    physical: PhysicalRecovery
    logical: LogicalRecovery | None
    events: tuple[RuntimeEvent, ...]
    backup_path: Path | None = None
    byte_offset: int | None = None
    message: str | None = None


@dataclass(frozen=True, slots=True)
class PreparedResume:
    """The safe session selected for continuation plus user-visible warnings."""

    session_id: str
    warnings: tuple[str, ...] = ()


def classify_logical_recovery(events: tuple[RuntimeEvent, ...]) -> LogicalRecovery:
    """Classify a structurally valid prefix without guessing missing side effects."""

    terminal_types = {RuntimeEventType.TURN_COMPLETED, RuntimeEventType.TURN_FAILED}
    last_terminal = max(
        (index for index, event in enumerate(events) if event.event_type in terminal_types),
        default=-1,
    )
    tail = events[last_terminal + 1 :]
    if not any(event.turn_id is not None for event in tail):
        return LogicalRecovery.CLEAN
    started = {
        event.payload.call_id: event.payload.risk
        for event in tail
        if isinstance(event.payload, ToolExecutionStartedPayload)
    }
    completed = {
        event.payload.result.call_id
        for event in tail
        if isinstance(event.payload, ToolExecutionCompletedPayload)
    }
    unresolved_risks = [risk for call_id, risk in started.items() if call_id not in completed]
    if any(risk in {None, RiskLevel.WRITE} for risk in unresolved_risks):
        return LogicalRecovery.UNCERTAIN_SIDE_EFFECT
    return LogicalRecovery.INTERRUPTED_SAFE


class SessionRecovery:
    """Combine physical recovery with a separate logical safety classification."""

    def __init__(self, store: RecoveryStorePort) -> None:
        self._store = store

    async def recover(self, session_id: str) -> RecoveryResult:
        physical = await self._store.recover_physical(session_id)
        logical = (
            None
            if physical.physical is PhysicalRecovery.UNRECOVERABLE
            else classify_logical_recovery(physical.events)
        )
        return RecoveryResult(
            physical=physical.physical,
            logical=logical,
            events=physical.events,
            backup_path=physical.backup_path,
            byte_offset=physical.byte_offset,
            message=physical.message,
        )


async def create_recovery_session(
    *,
    session: SessionPort,
    source_events: tuple[RuntimeEvent, ...],
    workspace_identity: str,
    new_session_id: str | None = None,
) -> str:
    """Create a standalone recovery snapshot through the last safe terminal event."""

    if not source_events:
        raise ValueError("Recovery source must contain events")
    terminal_types = {RuntimeEventType.TURN_COMPLETED, RuntimeEventType.TURN_FAILED}
    terminal_indexes = [
        index for index, event in enumerate(source_events) if event.event_type in terminal_types
    ]
    safe_index = terminal_indexes[-1] if terminal_indexes else 0
    safe_events = source_events[: safe_index + 1]
    actual_session_id = new_session_id or str(uuid4())
    journal = await EventJournal.open(session, actual_session_id)
    await journal.record(
        None,
        RuntimeEventType.SESSION_STARTED,
        SessionStartedPayload(
            name=f"recovery-{source_events[0].session_id[:8]}",
            workspace_identity=workspace_identity,
        ),
    )
    await journal.record(
        None,
        RuntimeEventType.RECOVERY_SNAPSHOT,
        RecoverySnapshotPayload(
            parent_session_id=source_events[0].session_id,
            through_sequence=safe_events[-1].sequence,
            messages=project_messages(safe_events),
        ),
    )
    return actual_session_id


async def close_interrupted_turn(
    *,
    session: SessionPort,
    events: tuple[RuntimeEvent, ...],
) -> RuntimeEvent:
    """Append one explicit terminal fact for a safe, incomplete turn."""

    if classify_logical_recovery(events) is not LogicalRecovery.INTERRUPTED_SAFE:
        raise ValueError("Only a safely interrupted turn can be closed automatically")
    terminal_types = {RuntimeEventType.TURN_COMPLETED, RuntimeEventType.TURN_FAILED}
    last_terminal = max(
        (index for index, event in enumerate(events) if event.event_type in terminal_types),
        default=-1,
    )
    tail = events[last_terminal + 1 :]
    turn_ids = [event.turn_id for event in tail if event.turn_id is not None]
    if not turn_ids:
        raise ValueError("Interrupted session has no active turn")
    active_turn_id = turn_ids[-1]
    if any(turn_id != active_turn_id for turn_id in turn_ids):
        raise ValueError("Interrupted session contains multiple open turns")
    model_calls = sum(isinstance(event.payload, AssistantMessageCompletedPayload) for event in tail)
    tool_calls = sum(isinstance(event.payload, ToolExecutionCompletedPayload) for event in tail)
    journal = await EventJournal.open(
        session,
        events[0].session_id,
        require_existing=True,
    )
    return await journal.record(
        active_turn_id,
        RuntimeEventType.TURN_FAILED,
        TurnFailedPayload(
            stop_reason=StopReason.CANCELLED,
            error_category="process_interrupted",
            message="Previous process ended before the turn completed",
            model_calls=model_calls,
            tool_calls=tool_calls,
        ),
    )


async def prepare_session_resume(
    *,
    session: SessionPort,
    recovery: SessionRecovery,
    session_id: str,
    workspace_identity: str,
    allow_recovery_snapshot: bool,
) -> PreparedResume:
    """Repair safe tails, close safe interruptions, and isolate uncertain writes."""

    result = await recovery.recover(session_id)
    if result.physical is PhysicalRecovery.UNRECOVERABLE:
        raise SessionError(
            SessionErrorCode.CORRUPT_EVENT,
            result.message or f"Session cannot be recovered: {session_id}",
        )
    warnings: list[str] = []
    if result.physical is PhysicalRecovery.REPAIRED_FINAL_PARTIAL_RECORD:
        warnings.append(result.message or "Repaired an incomplete final session record")
    if result.logical is LogicalRecovery.INTERRUPTED_SAFE:
        await close_interrupted_turn(session=session, events=result.events)
        warnings.append("Previous process interruption was recorded; resuming the session")
        return PreparedResume(session_id, tuple(warnings))
    if result.logical is LogicalRecovery.UNCERTAIN_SIDE_EFFECT:
        if not allow_recovery_snapshot:
            raise SessionError(
                SessionErrorCode.CORRUPT_EVENT,
                "A write side effect is uncertain; use interactive /resume to create "
                "a safe recovery session",
            )
        recovered_id = await create_recovery_session(
            session=session,
            source_events=result.events,
            workspace_identity=workspace_identity,
        )
        warnings.append(
            "A write side effect is uncertain; preserved the original and created "
            f"recovery-{session_id[:8]}"
        )
        return PreparedResume(recovered_id, tuple(warnings))
    return PreparedResume(session_id, tuple(warnings))
