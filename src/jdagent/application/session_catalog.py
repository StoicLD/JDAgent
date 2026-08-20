"""Rebuildable session discovery, selection, and naming use cases."""

from dataclasses import dataclass
from datetime import datetime

from jdagent.application.session_recovery import (
    LogicalRecovery,
    PhysicalRecovery,
    SessionRecovery,
)
from jdagent.domain.errors import SessionError, SessionErrorCode
from jdagent.domain.events import (
    RuntimeEvent,
    RuntimeEventType,
    SessionRenamedPayload,
    SessionStartedPayload,
    TurnCompletedPayload,
    TurnFailedPayload,
)
from jdagent.eventing import EventJournal
from jdagent.ports import SessionDiscoveryPort, SessionPort


class SessionSelectionError(ValueError):
    """A missing or ambiguous user-facing session selector."""


@dataclass(frozen=True, slots=True)
class SessionSummary:
    """Catalog projection rebuilt from canonical session facts."""

    session_id: str
    name: str
    workspace_identity: str
    updated_at: datetime
    status: str
    recovery_message: str | None = None

    @property
    def short_id(self) -> str:
        return self.session_id[:8]


def _summary(
    session_id: str,
    events: list[RuntimeEvent],
    workspace_identity: str,
) -> SessionSummary:
    if not events or not isinstance(events[0].payload, SessionStartedPayload):
        raise SessionError(
            SessionErrorCode.CORRUPT_EVENT,
            f"Session lacks a session_started fact: {session_id}",
        )
    started = events[0].payload
    if started.workspace_identity not in {None, workspace_identity}:
        raise SessionError(
            SessionErrorCode.CORRUPT_EVENT,
            f"Session belongs to another workspace: {session_id}",
        )
    name = started.name or f"session-{session_id[:8]}"
    status = "unknown"
    for event in events[1:]:
        if isinstance(event.payload, SessionRenamedPayload):
            name = event.payload.name
        elif isinstance(event.payload, TurnCompletedPayload):
            status = event.payload.stop_reason.value
        elif isinstance(event.payload, TurnFailedPayload):
            status = event.payload.stop_reason.value
    return SessionSummary(
        session_id=session_id,
        name=name,
        workspace_identity=workspace_identity,
        updated_at=events[-1].timestamp,
        status=status,
    )


class SessionCatalog:
    """Project session facts into discoverable summaries without an index dependency."""

    def __init__(
        self,
        *,
        session: SessionPort,
        discovery: SessionDiscoveryPort,
        workspace_identity: str,
        recovery: SessionRecovery | None = None,
    ) -> None:
        self._session = session
        self._discovery = discovery
        self._workspace_identity = workspace_identity
        self._recovery = recovery

    async def list_sessions(self) -> tuple[SessionSummary, ...]:
        summaries: list[SessionSummary] = []
        for session_id in await self._discovery.list_session_ids():
            recovery_message: str | None = None
            logical: LogicalRecovery | None = None
            if self._recovery is not None:
                result = await self._recovery.recover(session_id)
                if result.physical is PhysicalRecovery.UNRECOVERABLE:
                    raise SessionError(
                        SessionErrorCode.CORRUPT_EVENT,
                        result.message or f"Session cannot be recovered: {session_id}",
                    )
                events = list(result.events)
                logical = result.logical
                recovery_message = result.message
            else:
                events = [event async for event in self._session.read(session_id)]
            summary = _summary(session_id, events, self._workspace_identity)
            if logical is LogicalRecovery.INTERRUPTED_SAFE or logical is (
                LogicalRecovery.UNCERTAIN_SIDE_EFFECT
            ):
                summary = SessionSummary(
                    session_id=summary.session_id,
                    name=summary.name,
                    workspace_identity=summary.workspace_identity,
                    updated_at=summary.updated_at,
                    status=logical.value,
                    recovery_message=recovery_message,
                )
            elif recovery_message is not None:
                summary = SessionSummary(
                    session_id=summary.session_id,
                    name=summary.name,
                    workspace_identity=summary.workspace_identity,
                    updated_at=summary.updated_at,
                    status=summary.status,
                    recovery_message=recovery_message,
                )
            summaries.append(summary)
        summaries.sort(key=lambda item: item.updated_at, reverse=True)
        return tuple(summaries)

    async def resolve(self, selector: str) -> SessionSummary:
        normalized = selector.strip()
        if not normalized:
            raise SessionSelectionError("Session selector must not be empty")
        sessions = await self.list_sessions()
        exact = [item for item in sessions if item.session_id == normalized]
        if exact:
            return exact[0]
        matches = [
            item
            for item in sessions
            if item.name == normalized or item.session_id.startswith(normalized)
        ]
        if not matches:
            raise SessionSelectionError(f"Session not found: {normalized}")
        if len(matches) > 1:
            raise SessionSelectionError(f"Session selector is ambiguous: {normalized}")
        return matches[0]

    async def rename(self, session_id: str, name: str) -> SessionSummary:
        normalized = name.strip()
        if (
            not normalized
            or len(normalized) > 80
            or any(ord(character) < 32 for character in normalized)
        ):
            raise ValueError("Session name must be 1-80 printable characters")
        journal = await EventJournal.open(self._session, session_id, require_existing=True)
        await journal.record(
            None,
            RuntimeEventType.SESSION_RENAMED,
            SessionRenamedPayload(normalized),
        )
        events = list(journal.events)
        return _summary(session_id, events, self._workspace_identity)
