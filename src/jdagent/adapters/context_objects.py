"""Immutable, content-addressed objects for context manifests and original content."""

import asyncio
import hashlib
import os
import re
from pathlib import Path
from uuid import uuid4

from jdagent.domain.errors import SessionError, SessionErrorCode


class FileContextObjects:
    """Publish fsynced objects before any journal may refer to their digest."""

    def __init__(self, directory: Path) -> None:
        self._directory = directory.resolve()

    async def put(self, content: bytes) -> str:
        return await asyncio.to_thread(self.put_sync, content)

    async def get(self, digest: str) -> bytes:
        return await asyncio.to_thread(self.get_sync, digest)

    def get_sync(self, digest: str) -> bytes:
        if re.fullmatch(r"[0-9a-f]{64}", digest) is None:
            raise SessionError(SessionErrorCode.CORRUPT_EVENT, "Invalid context object hash")
        try:
            content = (self._directory / digest).read_bytes()
        except OSError as error:
            raise SessionError(
                SessionErrorCode.READ_FAILED, "Context object is unavailable"
            ) from error
        if hashlib.sha256(content).hexdigest() != digest:
            raise SessionError(SessionErrorCode.CORRUPT_EVENT, "Context object hash mismatch")
        return content

    def put_sync(self, content: bytes) -> str:
        digest = hashlib.sha256(content).hexdigest()
        self._directory.mkdir(parents=True, exist_ok=True)
        destination = self._directory / digest
        if destination.exists():
            self.get_sync(digest)
            return digest
        temporary = self._directory / f".{uuid4().hex}.tmp"
        try:
            with temporary.open("xb") as stream:
                if stream.write(content) != len(content):
                    raise OSError("Incomplete context object write")
                stream.flush()
                os.fsync(stream.fileno())
            try:
                os.link(temporary, destination)
            except FileExistsError:
                self.get_sync(digest)
        except OSError as error:
            raise SessionError(
                SessionErrorCode.APPEND_FAILED, "Could not save context object"
            ) from error
        finally:
            temporary.unlink(missing_ok=True)
        return digest
