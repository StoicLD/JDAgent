"""Content-addressed immutable source objects."""

from __future__ import annotations

import hashlib
import os
import shutil
from pathlib import Path

from jdagent.knowledge.errors import KnowledgeError, KnowledgeErrorCode

MAX_RAW_BYTES = 50 * 1024 * 1024
_HEX = 64


def content_digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class ContentAddressedStore:
    """Persist raw and parsed bytes by SHA-256. Owns no source lifecycle."""

    def __init__(self, root: Path) -> None:
        self._root = root
        self._root.mkdir(parents=True, exist_ok=True)

    def path_for(self, digest: str) -> Path:
        if len(digest) != _HEX or any(char not in "0123456789abcdef" for char in digest):
            raise KnowledgeError(KnowledgeErrorCode.INVALID_ARGUMENT, "Invalid content digest")
        return self._root / digest[:2] / digest

    def contains(self, digest: str) -> bool:
        return self.path_for(digest).is_file()

    def put(self, data: bytes) -> str:
        if len(data) > MAX_RAW_BYTES:
            raise KnowledgeError(
                KnowledgeErrorCode.SOURCE_TOO_LARGE,
                "Raw source artifact exceeds 50 MiB",
            )
        usage = shutil.disk_usage(self._root)
        if usage.free < len(data) + 4096:
            raise KnowledgeError(
                KnowledgeErrorCode.SOURCE_TOO_LARGE,
                "Not enough disk space for source object",
            )
        digest = content_digest(data)
        target = self.path_for(digest)
        if target.is_file():
            existing = target.read_bytes()
            if existing != data:
                raise KnowledgeError(
                    KnowledgeErrorCode.SOURCE_CORRUPT,
                    "Content-addressed object hash mismatch",
                )
            return digest
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.parent / f".{digest}.{os.getpid()}.tmp"
        try:
            with temporary.open("xb") as stream:
                written = stream.write(data)
                if written != len(data):
                    raise KnowledgeError(
                        KnowledgeErrorCode.SOURCE_CORRUPT,
                        "Incomplete source object write",
                    )
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, target)
        except FileExistsError:
            if self.contains(digest):
                return self.put(data)
            raise
        except OSError as error:
            raise KnowledgeError(
                KnowledgeErrorCode.SOURCE_CORRUPT,
                "Could not persist source object",
            ) from error
        finally:
            temporary.unlink(missing_ok=True)
        return digest

    def get(self, digest: str) -> bytes:
        path = self.path_for(digest)
        if not path.is_file():
            raise KnowledgeError(KnowledgeErrorCode.SOURCE_CORRUPT, "Source object is missing")
        data = path.read_bytes()
        if content_digest(data) != digest:
            raise KnowledgeError(
                KnowledgeErrorCode.SOURCE_CORRUPT,
                "Source object hash mismatch",
            )
        return data
