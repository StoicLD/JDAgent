"""Canonical event persistence and trace fan-out."""

from collections.abc import AsyncIterator

from jdagent.domain.errors import SessionError, SessionErrorCode
from jdagent.domain.events import (
    AssistantMessageCompletedPayload,
    MCPInvocationFinishedPayload,
    MCPInvocationStartedPayload,
    MCPTaskObservedPayload,
    MCPTaskRegisteredPayload,
    MCPTaskResultSavedPayload,
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


async def close_pending_calls(
    journal: RuntimeJournal,
    turn_id: str,
    *,
    reason: str = "Turn ended before a result was available",
) -> None:
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
    mcp_intents = {
        event.payload.call_id: event.payload
        for event in events
        if isinstance(event.payload, MCPInvocationStartedPayload)
    }
    mcp_finished = {
        event.payload.result.call_id: event.payload.result
        for event in events
        if isinstance(event.payload, MCPInvocationFinishedPayload)
    }
    task_references = {
        event.payload.call_id: event.payload
        for event in events
        if isinstance(event.payload, MCPTaskRegisteredPayload)
    }
    requested_calls = {
        call.call_id: call
        for event in events
        if isinstance(event.payload, AssistantMessageCompletedPayload)
        for call in event.payload.tool_calls
    }
    for event in journal.events:
        payload = event.payload
        if not isinstance(payload, MCPTaskResultSavedPayload) or payload.origin_turn_id != turn_id:
            continue
        registration = task_references.get(payload.call_id)
        call = requested_calls.get(payload.call_id)
        if registration is None or registration.task_ref != payload.task_ref or call is None:
            continue
        outcome = payload.outcome
        # Management may have obtained a result before interrupted model-call
        # pairing is repaired. Preserve any already recorded invocation ending.
        mcp_finished.setdefault(
            payload.call_id,
            ToolResult(
                payload.call_id,
                call.name,
                ToolResultStatus.ERROR if outcome.error_code else ToolResultStatus.SUCCESS,
                error_code=outcome.error_code,
                error_message=outcome.message or None,
                outcome=outcome,
                metadata={"tool_origin": outcome.data.get("source")},
            ),
        )
    task_statuses = {
        event.payload.task_ref: event.payload.status
        for event in journal.events
        if isinstance(event.payload, MCPTaskObservedPayload) and event.payload.status is not None
    }
    for event in events:
        if not isinstance(event.payload, AssistantMessageCompletedPayload):
            continue
        for call in event.payload.tool_calls:
            if call.call_id in completed:
                continue
            unknown = call.call_id in started
            intent = mcp_intents.get(call.call_id)
            task = task_references.get(call.call_id)
            outcome = ToolOutcome(
                data={
                    "task_ref": task.task_ref,
                    "task_status": task_statuses.get(task.task_ref, task.status),
                }
                if task is not None
                else {},
                completed=False,
                effect="unknown" if unknown else "none",
                error_code=ToolErrorCode.UNCERTAIN
                if unknown or intent
                else ToolErrorCode.NOT_EXECUTED,
                message=reason,
                requires_stop=intent is not None,
            )
            result = mcp_finished.get(call.call_id) or ToolResult(
                call.call_id,
                call.name,
                ToolResultStatus.ERROR,
                error_code=outcome.error_code,
                error_message=outcome.message,
                outcome=outcome,
                metadata={
                    "tool_origin": {
                        "kind": "mcp",
                        "server_id": intent.server_id,
                        "raw_name": intent.raw_name,
                        "generation": intent.generation,
                    }
                }
                if intent
                else {},
            )
            await journal.record(
                turn_id,
                RuntimeEventType.TOOL_EXECUTION_COMPLETED,
                ToolExecutionCompletedPayload(result),
            )
            completed.add(call.call_id)
