import asyncio
import hashlib
import json
from dataclasses import asdict
from pathlib import Path

import pytest

from jdagent.adapters.jsonl_recovery import JsonlRecoveryStore
from jdagent.adapters.jsonl_session import JsonlSession
from jdagent.adapters.memory import InMemoryContextObjects
from jdagent.context_archive import archive_request, encode_json, prefix_hash, reconstruct_request
from jdagent.domain.errors import SessionError
from jdagent.domain.events import (
    CompactionFailedPayload,
    RuntimeEvent,
    RuntimeEventType,
    UserMessagePayload,
)
from jdagent.domain.model import MessageRole, ModelMessage, ModelRequest, SystemPart


def test_logical_request_reconstruction_is_exact() -> None:
    async def scenario() -> None:
        objects = InMemoryContextObjects()
        request = ModelRequest(
            "fake",
            (ModelMessage(MessageRole.USER, "用户原文"),),
            (SystemPart("项目规则", "directory_instructions"),),
            metadata={"request_id": "r"},
        )
        digest = await archive_request(request, objects, None)
        assert await reconstruct_request(digest, objects) == request

    asyncio.run(scenario())


def test_original_v05_failed_event_keeps_its_prefix_hash(tmp_path: Path):
    async def scenario():
        event = RuntimeEvent.create(
            session_id="old-v05",
            turn_id="t",
            sequence=1,
            event_type=RuntimeEventType.COMPACTION_FAILED,
            payload=CompactionFailedPayload(1, "source", "CompactionError"),
        )
        legacy = asdict(event)
        legacy["payload"].pop("response_hashes")
        expected = hashlib.sha256(encode_json([legacy])).hexdigest()
        session = JsonlSession(tmp_path)
        session.path_for("old-v05").write_bytes(encode_json(legacy) + b"\n")
        restored = [item async for item in session.read("old-v05")]
        assert prefix_hash(restored, 1) == expected
        assert restored[0] == event

    asyncio.run(scenario())


def test_large_original_is_hashed_verified_and_recoverable(tmp_path: Path):
    asyncio.run(_large_original(tmp_path))


async def _large_original(tmp_path: Path):
    session = JsonlSession(tmp_path)
    event = RuntimeEvent.create(
        session_id="large",
        turn_id="t",
        sequence=1,
        event_type=RuntimeEventType.USER_MESSAGE,
        payload=UserMessagePayload("完整原文" * 10000),
    )
    await session.append(event)
    envelope = json.loads(session.path_for("large").read_text(encoding="utf-8"))
    assert "payload" not in envelope
    assert session.path_for("large").stat().st_size < 1024
    assert [item async for item in session.read("large")] == [event]
    assert (await JsonlRecoveryStore(tmp_path).recover_physical("large")).events == (event,)
    (tmp_path / "objects" / envelope["owned_payload_hash"]).write_bytes(b"tampered")
    with pytest.raises(SessionError, match="hash mismatch"):
        _ = [item async for item in session.read("large")]
