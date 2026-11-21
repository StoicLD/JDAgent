"""从旧版工作区导入 v0.1 会话，保留原始会话文件。"""

import asyncio
import os
from pathlib import Path

from jdagent.adapters.jsonl_session import JsonlSession
from jdagent.domain.errors import SessionError, SessionErrorCode


def _copy_atomic(source: Path, target: Path) -> None:
    raw = source.read_bytes()
    temporary = target.parent / f".{target.name}-{os.getpid()}.tmp"
    try:
        with temporary.open("xb") as stream:
            written = stream.write(raw)
            if written != len(raw):
                raise OSError("Incomplete legacy session copy")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


def _prepare_import(legacy_directory: Path, target_directory: Path) -> bool:
    if not legacy_directory.exists():
        return False
    target_directory.mkdir(parents=True, exist_ok=True)
    return True


def _compare_existing(source: Path, target: Path) -> bool | None:
    if not target.exists():
        return None
    return source.read_bytes() == target.read_bytes()


async def import_legacy_sessions(
    legacy_directory: Path,
    target_directory: Path,
) -> tuple[str, ...]:
    """验证旧工作区中的会话文件并复制到目标位置，保留原文件供核对。"""

    if not await asyncio.to_thread(_prepare_import, legacy_directory, target_directory):
        return ()
    legacy = JsonlSession(legacy_directory)
    identifiers = await legacy.list_session_ids()
    imported: list[str] = []
    for session_id in identifiers:
        _ = [event async for event in legacy.read(session_id)]
        source = legacy_directory / f"{session_id}.jsonl"
        target = target_directory / source.name
        try:
            identical = await asyncio.to_thread(_compare_existing, source, target)
        except OSError as error:
            raise SessionError(
                SessionErrorCode.READ_FAILED,
                f"Could not compare legacy session: {session_id}",
            ) from error
        if identical is not None:
            if not identical:
                raise SessionError(
                    SessionErrorCode.APPEND_FAILED,
                    f"Legacy session conflicts with existing target: {session_id}",
                )
            continue
        try:
            await asyncio.to_thread(_copy_atomic, source, target)
        except OSError as error:
            raise SessionError(
                SessionErrorCode.APPEND_FAILED,
                f"Could not import legacy session: {session_id}",
            ) from error
        imported.append(session_id)
    return tuple(imported)
