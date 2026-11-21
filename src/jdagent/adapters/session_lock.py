"""使用操作系统文件锁独占会话；持锁进程退出时锁会释放。"""

import os
from pathlib import Path
from typing import BinaryIO

from jdagent.domain.errors import SessionError, SessionErrorCode


class SessionLease:
    """持有会话锁文件的非阻塞操作系统锁。进程退出时锁自动释放，锁文件本身保持原位。"""

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
