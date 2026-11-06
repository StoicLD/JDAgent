"""Rebuildable session discovery, selection, and naming use cases."""

from dataclasses import dataclass, replace
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
    name: str | None
    workspace_identity: str | None
    updated_at: datetime | None
    status: str
    recovery_message: str | None = None
    read_error: SessionErrorCode | None = None

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
            try:
                summary = await self._read_summary(session_id)
            except SessionError as error:
                summary = SessionSummary(
                    session_id=session_id,
                    name=None,
                    workspace_identity=None,
                    updated_at=None,
                    status=error.code.value,
                    read_error=error.code,
                    recovery_message=f"Session is unavailable: {error.code.value}",
                )
            summaries.append(summary)
        summaries.sort(
            key=lambda item: (
                item.updated_at is not None,
                item.updated_at.timestamp() if item.updated_at is not None else 0,
                item.session_id,
            ),
            reverse=True,
        )
        return tuple(summaries)

    async def _read_summary(self, session_id: str) -> SessionSummary:
        recovery_message: str | None = None
        logical: LogicalRecovery | None = None
        if self._recovery is not None:
            result = await self._recovery.recover(session_id)
            if result.physical is PhysicalRecovery.UNRECOVERABLE:
                raise SessionError(SessionErrorCode.CORRUPT_EVENT, "Session cannot be recovered")
            events = list(result.events)
            logical = result.logical
            recovery_message = result.message
        else:
            events = [event async for event in self._session.read(session_id)]
        summary = _summary(session_id, events, self._workspace_identity)
        return replace(
            summary,
            status=logical.value
            if logical is LogicalRecovery.INTERRUPTED_SAFE
            or logical is LogicalRecovery.UNCERTAIN_SIDE_EFFECT
            else summary.status,
            recovery_message=recovery_message,
        )

    @staticmethod
    def _readable(summary: SessionSummary) -> SessionSummary:
        if summary.read_error is not None:
            raise SessionError(
                summary.read_error, f"Session {summary.session_id} is unavailable: {summary.status}"
            )
        return summary

    async def resolve(self, selector: str) -> SessionSummary:
        normalized = selector.strip()
        if not normalized:
            raise SessionSelectionError("Session selector must not be empty")
        sessions = await self.list_sessions()
        exact = [item for item in sessions if item.session_id == normalized]
        if exact:
            return self._readable(exact[0])
        matches = [
            item
            for item in sessions
            if item.name == normalized or item.session_id.startswith(normalized)
        ]
        if not matches:
            raise SessionSelectionError(f"Session not found: {normalized}")
        if len(matches) > 1:
            raise SessionSelectionError(f"Session selector is ambiguous: {normalized}")
        return self._readable(matches[0])

    async def rename(self, session_id: str, name: str) -> SessionSummary:
        normalized = name.strip()
        if (
            not normalized
            or len(normalized) > 80
            or any(ord(character) < 32 for character in normalized)
        ):
            raise ValueError("Session name must be 1-80 printable characters")
        journal = await EventJournal.open(self._session, session_id, require_existing=True)
        _summary(session_id, list(journal.events), self._workspace_identity)
        await journal.record(
            None,
            RuntimeEventType.SESSION_RENAMED,
            SessionRenamedPayload(normalized),
        )
        events = list(journal.events)
        return _summary(session_id, events, self._workspace_identity)
