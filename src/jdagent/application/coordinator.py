"""Coordinate one turn without embedding adapter details in the agent loop."""

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol
from uuid import uuid4

from jdagent.core.loop import AgentLoop, CancellationToken, TurnResult
from jdagent.domain.events import RuntimeEventType, SessionStartedPayload, UserMessagePayload
from jdagent.domain.tools import ToolExecutionContext
from jdagent.eventing import EventJournal
from jdagent.observability import TraceProjection
from jdagent.ports import EventObserver, RuntimeJournal, SessionPort


class AgentLoopFactory(Protocol):
    """Create a configured loop for a per-session journal."""

    def create(self, journal: RuntimeJournal) -> AgentLoop: ...


@dataclass(frozen=True, slots=True)
class CoordinatedTurn:
    """Application result with stable IDs and its trace projection."""

    session_id: str
    turn_id: str
    result: TurnResult
    trace: TraceProjection


class TurnCoordinator:
    """Own the session-input-to-turn-result application lifecycle."""

    def __init__(
        self,
        *,
        session: SessionPort,
        workspace: Path,
        loop_factory: AgentLoopFactory,
        event_observers: tuple[EventObserver, ...] = (),
    ) -> None:
        self._session = session
        self._workspace = workspace
        self._loop_factory = loop_factory
        self._event_observers = event_observers

    async def send(
        self,
        user_text: str,
        *,
        session_id: str | None = None,
        cancellation: CancellationToken | None = None,
    ) -> CoordinatedTurn:
        """Append a user message and drive exactly one agent turn."""

        if not user_text.strip():
            raise ValueError("user_text must not be empty")
        actual_session_id = session_id or str(uuid4())
        turn_id = str(uuid4())
        trace = TraceProjection()
        journal = await EventJournal.open(
            self._session,
            actual_session_id,
            observers=(trace, *self._event_observers),
            require_existing=session_id is not None,
        )
        if not journal.events:
            await journal.record(None, RuntimeEventType.SESSION_STARTED, SessionStartedPayload())
        await journal.record(
            turn_id,
            RuntimeEventType.USER_MESSAGE,
            UserMessagePayload(user_text),
        )

        loop = self._loop_factory.create(journal)
        result = await loop.run(
            turn_id,
            ToolExecutionContext(actual_session_id, turn_id, self._workspace),
            cancellation=cancellation,
        )
        return CoordinatedTurn(actual_session_id, turn_id, result, trace)
