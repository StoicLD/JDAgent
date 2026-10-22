import asyncio
from pathlib import Path

import pytest

from jdagent.adapters.context_objects import FileContextObjects
from jdagent.adapters.jsonl_session import JsonlSession
from jdagent.domain.errors import SessionError


def test_context_objects_survive_reopen_and_detect_corruption(tmp_path: Path) -> None:
    async def scenario() -> None:
        store = FileContextObjects(tmp_path / "objects")
        digest = await store.put(b"original tool output")
        assert await store.put(b"original tool output") == digest
        reopened = FileContextObjects(tmp_path / "objects")
        assert await reopened.get(digest) == b"original tool output"
        (tmp_path / "objects" / digest).write_bytes(b"modified")
        with pytest.raises(SessionError, match="hash"):
            await reopened.get(digest)
        with pytest.raises(SessionError):
            await reopened.get("../secret")

    asyncio.run(scenario())


def test_session_owner_excludes_other_instances_until_release(tmp_path: Path) -> None:
    async def scenario() -> None:
        first = JsonlSession(tmp_path)
        second = JsonlSession(tmp_path)
        await first.acquire("owned")
        with pytest.raises(SessionError, match="busy"):
            await second.acquire("owned")
        await first.release("owned")
        await second.acquire("owned")
        await second.release("owned")

    asyncio.run(scenario())
