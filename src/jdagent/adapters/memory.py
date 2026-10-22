"""Process-local session and event adapters."""

import asyncio
import hashlib
from collections.abc import AsyncIterator

from jdagent.domain.errors import SessionError, SessionErrorCode
from jdagent.domain.events import RuntimeEvent


class InMemorySession:
    """Stores canonical events in strict session sequence order."""

    def __init__(self) -> None:
        self._events: dict[str, list[RuntimeEvent]] = {}
        self._lock = asyncio.Lock()

    async def acquire(self, session_id: str) -> None:
        """This isolated adapter has no external process sharing its data."""

    async def release(self, session_id: str) -> None:
        """No operating-system resource is owned by the in-memory adapter."""

    async def append(self, event: RuntimeEvent) -> None:
        async with self._lock:
            events = self._events.setdefault(event.session_id, [])
            expected = len(events) + 1
            if event.sequence != expected:
                raise SessionError(
                    SessionErrorCode.APPEND_FAILED,
                    f"Expected sequence {expected}, received {event.sequence}",
                )
            events.append(event)

    async def read(self, session_id: str) -> AsyncIterator[RuntimeEvent]:
        async with self._lock:
            if session_id not in self._events:
                raise SessionError(SessionErrorCode.NOT_FOUND, f"Session not found: {session_id}")
            snapshot = tuple(self._events[session_id])
        for event in snapshot:
            yield event

    async def list_session_ids(self) -> tuple[str, ...]:
        """Return stable identifiers for catalog projection tests."""

        async with self._lock:
            return tuple(sorted(self._events))


class InMemoryContextObjects:
    """Immutable object adapter for deterministic runtime tests."""

    def __init__(self) -> None:
        self._objects: dict[str, bytes] = {}

    async def put(self, content: bytes) -> str:
        digest = hashlib.sha256(content).hexdigest()
        self._objects[digest] = content
        return digest

    async def get(self, digest: str) -> bytes:
        try:
            return self._objects[digest]
        except KeyError as error:
            raise SessionError(
                SessionErrorCode.READ_FAILED, "Context object is unavailable"
            ) from error
