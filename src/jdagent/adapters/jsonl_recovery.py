"""Explicit JSONL final-record recovery adapter."""

import asyncio
import hashlib
import json
import os
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

from jdagent.adapters.jsonl_session import (
    JsonlSession,
    session_file_lock,
)
from jdagent.adapters.session_lock import SessionLease
from jdagent.application.session_recovery import (
    PhysicalRecovery,
    PhysicalRecoveryResult,
)
from jdagent.cancellation import complete_before_cancel
from jdagent.domain.errors import SessionError, SessionErrorCode
from jdagent.domain.events import RuntimeEvent
from jdagent.domain.json import JsonObject, normalize_json


def _parse_prefix(raw: bytes, session_id: str, session: JsonlSession) -> tuple[RuntimeEvent, ...]:
    events: list[RuntimeEvent] = []
    offset = 0
    for line in raw.splitlines(keepends=True):
        if not line.endswith(b"\n"):
            raise ValueError("Prefix contains an incomplete record")
        loaded = cast(object, json.loads(line[:-1].decode("utf-8")))
        data = normalize_json(loaded)
        if not isinstance(data, dict):
            raise ValueError("JSONL event must be an object")
        event = session.decode_record(cast(JsonObject, data))
        expected = len(events) + 1
        if event.session_id != session_id or event.sequence != expected:
            raise ValueError(f"Invalid session or sequence at byte offset {offset}")
        events.append(event)
        offset += len(line)
    if not events:
        raise ValueError("No complete event prefix")
    return tuple(events)


def _write_fsynced(path: Path, raw: bytes) -> None:
    with path.open("xb") as stream:
        written = stream.write(raw)
        if written != len(raw):
            raise OSError("Incomplete recovery write")
        stream.flush()
        os.fsync(stream.fileno())


def _repair_sync(directory: Path, session_id: str) -> PhysicalRecoveryResult:
    session = JsonlSession(directory)
    path = session.path_for(session_id)
    lease = SessionLease(path.with_suffix(".lock"))
    try:
        with session_file_lock(path):
            return _repair_locked(directory, session_id, path)
    finally:
        lease.close()


def _repair_locked(
    directory: Path,
    session_id: str,
    path: Path,
) -> PhysicalRecoveryResult:
    if not path.exists():
        raise SessionError(SessionErrorCode.NOT_FOUND, f"Session not found: {session_id}")
    try:
        raw = path.read_bytes()
    except OSError as error:
        raise SessionError(SessionErrorCode.READ_FAILED, "Could not read session") from error

    if raw.endswith(b"\n"):
        try:
            events = _parse_prefix(raw, session_id, JsonlSession(directory))
        except SessionError as error:
            if error.code is SessionErrorCode.UNSUPPORTED_SCHEMA:
                raise
            return PhysicalRecoveryResult(
                PhysicalRecovery.UNRECOVERABLE,
                (),
                message=str(error),
            )
        except (
            UnicodeDecodeError,
            json.JSONDecodeError,
            TypeError,
            ValueError,
        ) as error:
            return PhysicalRecoveryResult(
                PhysicalRecovery.UNRECOVERABLE,
                (),
                message=str(error),
            )
        return PhysicalRecoveryResult(PhysicalRecovery.NO_REPAIR_NEEDED, events)

    offset = raw.rfind(b"\n") + 1
    prefix = raw[:offset]
    try:
        events = _parse_prefix(prefix, session_id, JsonlSession(directory))
    except SessionError as error:
        if error.code is SessionErrorCode.UNSUPPORTED_SCHEMA:
            raise
        return PhysicalRecoveryResult(
            PhysicalRecovery.UNRECOVERABLE,
            (),
            byte_offset=offset,
            message=f"Session cannot be repaired safely: {error}",
        )
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError) as error:
        return PhysicalRecoveryResult(
            PhysicalRecovery.UNRECOVERABLE,
            (),
            byte_offset=offset,
            message=f"Session cannot be repaired safely: {error}",
        )

    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S.%fZ")
    digest = hashlib.sha256(raw).hexdigest()[:12]
    backup = directory / f"{session_id}.backup-{timestamp}-{digest}.bak"
    temporary = directory / f".{session_id}.repair-{os.getpid()}.tmp"
    try:
        _write_fsynced(backup, raw)
        _write_fsynced(temporary, prefix)
        os.replace(temporary, path)
    except OSError as error:
        raise SessionError(SessionErrorCode.APPEND_FAILED, "Could not repair session") from error
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
    return PhysicalRecoveryResult(
        PhysicalRecovery.REPAIRED_FINAL_PARTIAL_RECORD,
        events,
        backup_path=backup,
        byte_offset=offset,
        message="Removed one incomplete final JSONL record after preserving a backup",
    )


class JsonlRecoveryStore:
    """Storage adapter for explicit, evidence-preserving JSONL repair."""

    def __init__(self, directory: Path, session: JsonlSession | None = None) -> None:
        self._directory = directory.resolve(strict=False)
        self._session = session or JsonlSession(self._directory)

    @asynccontextmanager
    async def ownership(self, session_id: str) -> AsyncGenerator[None]:
        already_owned = self._session.owns(session_id)
        try:
            await complete_before_cancel(self._session.acquire(session_id))
            yield
        finally:
            if not already_owned:
                await complete_before_cancel(self._session.release(session_id))

    async def recover_physical(self, session_id: str) -> PhysicalRecoveryResult:
        if self._session.owns(session_id):
            path = self._session.path_for(session_id)

            def repair_owned() -> PhysicalRecoveryResult:
                with session_file_lock(path):
                    return _repair_locked(self._directory, session_id, path)

            return await asyncio.to_thread(repair_owned)
        return await asyncio.to_thread(_repair_sync, self._directory, session_id)
