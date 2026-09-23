"""Canonical event persistence and trace fan-out."""

from collections.abc import AsyncIterator

from jdagent.domain.errors import SessionError, SessionErrorCode
from jdagent.domain.events import (
    AssistantMessageCompletedPayload,
    RuntimeEvent,
    RuntimeEventType,
    RuntimePayload,
    ToolExecutionCompletedPayload,
    ToolExecutionStartedPayload,
)
from jdagent.domain.tools import RiskLevel, ToolErrorCode, ToolOutcome, ToolResult, ToolResultStatus
from jdagent.ports import EventObserver, RuntimeJournal, SessionPort


async def _collect(items: AsyncIterator[RuntimeEvent]) -> list[RuntimeEvent]:
    return [item async for item in items]


class EventJournal:
    """Sequence events, persist them first, then notify trace observers."""

    def __init__(
        self,
        session: SessionPort,
        session_id: str,
        existing: list[RuntimeEvent],
        observers: tuple[EventObserver, ...],
    ) -> None:
        self._session = session
        self._session_id = session_id
        self._events = existing
        self._observers = observers

    @classmethod
    async def open(
        cls,
        session: SessionPort,
        session_id: str,
        *,
        observers: tuple[EventObserver, ...] = (),
        require_existing: bool = False,
    ) -> "EventJournal":
        """Open an existing event sequence or prepare an empty new session."""

        try:
            existing = await _collect(session.read(session_id))
        except SessionError as error:
            if error.code is not SessionErrorCode.NOT_FOUND or require_existing:
                raise
            existing = []
        return cls(session, session_id, existing, observers)

    @property
    def events(self) -> tuple[RuntimeEvent, ...]:
        return tuple(self._events)

    async def record(
        self,
        turn_id: str | None,
        event_type: RuntimeEventType,
        payload: RuntimePayload,
    ) -> RuntimeEvent:
        """Create and emit the next canonical event."""

        event = RuntimeEvent.create(
            session_id=self._session_id,
            turn_id=turn_id,
            sequence=len(self._events) + 1,
            event_type=event_type,
            payload=payload,
        )
        await self.emit(event)
        return event

    async def emit(self, event: RuntimeEvent) -> None:
        """Persist a caller-created event and then notify observers."""

        if event.session_id != self._session_id:
            raise SessionError(SessionErrorCode.APPEND_FAILED, "Event belongs to another session")
        expected = len(self._events) + 1
        if event.sequence != expected:
            raise SessionError(
                SessionErrorCode.APPEND_FAILED,
                f"Expected journal sequence {expected}, received {event.sequence}",
            )
        await self._session.append(event)
        self._events.append(event)
        for observer in self._observers:
            await observer.observe(event)


async def close_pending_calls(journal: RuntimeJournal, turn_id: str) -> None:
    """Record explicit non-execution for unstarted calls before ending a batch."""
    events = [event for event in journal.events if event.turn_id == turn_id]
    completed = {
        event.payload.result.call_id
        for event in events
        if isinstance(event.payload, ToolExecutionCompletedPayload)
    }
    started = {
        event.payload.call_id
        for event in events
        if isinstance(event.payload, ToolExecutionStartedPayload)
        and event.payload.risk in {RiskLevel.WRITE, RiskLevel.EXECUTE}
    }
    for event in events:
        if not isinstance(event.payload, AssistantMessageCompletedPayload):
            continue
        for call in event.payload.tool_calls:
            if call.call_id in completed:
                continue
            unknown = call.call_id in started
            outcome = ToolOutcome(
                completed=False,
                effect="unknown" if unknown else "none",
                error_code=ToolErrorCode.UNCERTAIN if unknown else ToolErrorCode.NOT_EXECUTED,
                message="Turn ended before a result was available",
            )
            await journal.record(
                turn_id,
                RuntimeEventType.TOOL_EXECUTION_COMPLETED,
                ToolExecutionCompletedPayload(
                    ToolResult(
                        call.call_id,
                        call.name,
                        ToolResultStatus.ERROR,
                        error_code=outcome.error_code,
                        error_message=outcome.message,
                        outcome=outcome,
                    )
                ),
            )
            completed.add(call.call_id)
