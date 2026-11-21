"""将会话和事件保存在当前进程的内存里。"""

import asyncio
import hashlib
from collections.abc import AsyncIterator

from jdagent.domain.errors import SessionError, SessionErrorCode
from jdagent.domain.events import RuntimeEvent


class InMemorySession:
    """将会话事件保存在当前进程内，并检查追加顺序，适用于无需落盘的测试。"""

    def __init__(self) -> None:
        self._events: dict[str, list[RuntimeEvent]] = {}
        self._lock = asyncio.Lock()

    async def acquire(self, session_id: str) -> None:
        """内存会话只在当前进程内使用，取得会话时无需申请操作系统文件锁。"""

    async def release(self, session_id: str) -> None:
        """内存会话未占用操作系统文件锁，释放操作可直接完成。"""

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
        """返回顺序稳定的会话标识，供测试重建会话目录。"""

        async with self._lock:
            return tuple(sorted(self._events))


class InMemoryContextObjects:
    """用内存实现按内容摘要存取的不可变对象接口，供离线测试重建请求。"""

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
