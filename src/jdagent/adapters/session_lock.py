"""Operating-system ownership locks, released automatically after process death."""

import os
from pathlib import Path
from typing import BinaryIO

from jdagent.domain.errors import SessionError, SessionErrorCode


class SessionLease:
    """Hold one nonblocking OS lock; the lock file is never deleted or replaced."""

    def __init__(self, path: Path) -> None:
        self._stream: BinaryIO | None = None
        stream = path.open("a+b")
        try:
            if os.name == "nt":
                import msvcrt

                if stream.seek(0, os.SEEK_END) == 0:
                    stream.write(b"\0")
                    stream.flush()
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            stream.close()
            raise SessionError(
                SessionErrorCode.BUSY, "session_busy: another process owns this session"
            ) from error
        self._stream = stream

    def close(self) -> None:
        if self._stream is not None:
            self._stream.close()
            self._stream = None
