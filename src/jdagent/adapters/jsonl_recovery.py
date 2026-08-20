"""Explicit JSONL final-record recovery adapter."""

import asyncio
import hashlib
import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

from jdagent.adapters.jsonl_session import (
    JsonlSession,
    event_from_data,
    session_file_lock,
)
from jdagent.application.session_recovery import (
    PhysicalRecovery,
    PhysicalRecoveryResult,
)
from jdagent.domain.errors import SessionError, SessionErrorCode
from jdagent.domain.events import RuntimeEvent
from jdagent.domain.json import JsonObject, normalize_json


def _parse_prefix(raw: bytes, session_id: str) -> tuple[RuntimeEvent, ...]:
    events: list[RuntimeEvent] = []
    offset = 0
    for line in raw.splitlines(keepends=True):
        if not line.endswith(b"\n"):
            raise ValueError("Prefix contains an incomplete record")
        loaded = cast(object, json.loads(line[:-1].decode("utf-8")))
        data = normalize_json(loaded)
        if not isinstance(data, dict):
            raise ValueError("JSONL event must be an object")
        event = event_from_data(cast(JsonObject, data))
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
    with session_file_lock(path):
        return _repair_locked(directory, session_id, path)


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
            events = _parse_prefix(raw, session_id)
        except (
            SessionError,
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
        events = _parse_prefix(prefix, session_id)
    except (SessionError, UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError) as error:
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

    def __init__(self, directory: Path) -> None:
        self._directory = directory.resolve(strict=False)

    async def recover_physical(self, session_id: str) -> PhysicalRecoveryResult:
        return await asyncio.to_thread(_repair_sync, self._directory, session_id)
