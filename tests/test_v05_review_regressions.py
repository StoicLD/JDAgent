"""Regression cases found by the independent v0.5 implementation review."""

import asyncio
import json
import time
from pathlib import Path

import pytest

from jdagent.adapters.fake import FakeModelPort
from jdagent.adapters.jsonl_session import JsonlSession
from jdagent.adapters.memory import InMemoryContextObjects, InMemorySession
from jdagent.context import ContextBuilder
from jdagent.context_budget import ContextBudget
from jdagent.context_runtime import CompactionError, ContextService
from jdagent.domain.errors import StopReason
from jdagent.domain.events import (
    ConstraintRevokedPayload,
    RuntimeEventType,
    SessionStartedPayload,
    TurnCompletedPayload,
    UserMessagePayload,
)
from jdagent.domain.model import ModelSettings, ResponseCompleted, TextDelta
from jdagent.eventing import EventJournal
from jdagent.knowledge.embedding import EmbeddingKind, EmbeddingResult
from jdagent.knowledge.types import EmbeddingProfile
from jdagent.maintenance import MemoryMaintenance
from jdagent.memory_service import MemoryService
from jdagent.memory_store import MemoryStore


def test_cancellation_does_not_exhaust_three_failure_retries(tmp_path: Path):
    store = MemoryStore(tmp_path / "memory.sqlite")
    job = store.schedule("w", "s", 1)
    for index in range(5):
        lease = store.claim("w", "owner", now=index + 1)
        assert lease
        store.finish(lease, now=index + 1, error="cancelled", cancelled=True)
    assert store.job_status(job) == "pending"
    for index in range(3):
        lease = store.claim("w", "owner", now=100 + index * 100)
        assert lease
        store.finish(lease, now=100 + index * 100, error="invalid response")
    assert store.job_status(job) == "failed"


def test_maintenance_and_total_caps_share_atomic_manifest_reservation(tmp_path: Path):
    store = MemoryStore(tmp_path / "memory.sqlite")
    store.reserve(
        "task",
        "w",
        "task",
        60,
        2,
        input_limit=100,
        output_limit=10,
        maintenance_input_limit=20,
        maintenance_output_limit=5,
        manifest="task-hash",
    )
    with pytest.raises(ValueError, match="maintenance token"):
        store.reserve(
            "too-large",
            "w",
            "job",
            21,
            1,
            input_limit=100,
            output_limit=10,
            maintenance_input_limit=20,
            manifest="unused",
        )
    store.reserve(
        "job",
        "w",
        "job",
        20,
        1,
        input_limit=100,
        output_limit=10,
        maintenance_input_limit=20,
        manifest="job-hash",
    )
    with pytest.raises(ValueError, match="total token"):
        store.reserve("later", "w", "task", 21, 1, input_limit=100, output_limit=10)
    assert store.costs("w") == (80, 3)


def test_expiry_dependency_and_disabled_correction_lifecycle(tmp_path: Path):
    store = MemoryStore(tmp_path / "memory.sqlite")
    intent = store.accept_intent(
        "记住：临时计划 [有效至：2000-01-01T00:00:00Z]",
        workspace="w",
        session="s",
        turn="t",
        event="e",
    )
    assert intent
    item = store.apply_intent(intent, workspace="w", session="s", turn="t", write_allowed=True)
    assert item.state == "expired" and not store.recall("w")
    intent = store.accept_intent(
        "记住：政策事实",
        workspace="w",
        session="s",
        turn="t",
        event="e2",
        dependencies=("source:v1",),
    )
    assert intent
    item = store.apply_intent(
        intent, workspace="w", session="s", turn="t", write_allowed=True, dependencies_verified=True
    )
    store.invalidate_dependency("source:v1")
    assert store.versions(item.memory_id)[-1].state == "needs_verification"
    assert store.versions(item.memory_id)[0].state == "superseded"
    for event, command in [
        ("e3", f"忘记本项目记忆 {item.memory_id}"),
        ("e4", f"把本项目记忆 {item.memory_id} 更正为：仍然停用"),
    ]:
        intent = store.accept_intent(command, workspace="w", session="s", turn="t", event=event)
        assert intent
        item = store.apply_intent(intent, workspace="w", session="s", turn="t", write_allowed=True)
        assert item.state == "disabled"


def test_explicit_revocation_unblocks_first_oversized_input():
    async def scenario():
        journal = await EventJournal.open(InMemorySession(), "s")
        await journal.record(None, RuntimeEventType.SESSION_STARTED, SessionStartedPayload())
        original = await journal.record(
            "t1", RuntimeEventType.USER_MESSAGE, UserMessagePayload("x" * 10000)
        )
        service = ContextService(
            builder=ContextBuilder(model="fake", settings=ModelSettings(max_output_tokens=100)),
            model=FakeModelPort(scripts=((ResponseCompleted("stop"),),)),
            journal=journal,
            objects=InMemoryContextObjects(),
            budget=ContextBudget(4096, output_tokens=100, safety_tokens=100),
        )
        with pytest.raises(CompactionError, match="C2"):
            await service.prepare_request()
        user = await journal.record(
            "t2", RuntimeEventType.USER_MESSAGE, UserMessagePayload("撤销本会话约束 C2")
        )
        await journal.record(
            "t2",
            RuntimeEventType.CONSTRAINT_REVOKED,
            ConstraintRevokedPayload(original.sequence, user.event_id),
        )
        request = await service.prepare_request()
        assert request.messages[-1].content == "撤销本会话约束 C2"
        assert all("x" * 100 not in message.content for message in request.messages)
        assert original in journal.events

    asyncio.run(scenario())


def test_irrelevant_semantic_vectors_do_not_inject_memory(tmp_path: Path):
    class OrthogonalEmbedding:
        async def embed(
            self, texts: tuple[str, ...], profile: EmbeddingProfile, *, input_kind: EmbeddingKind
        ):
            vector = (1.0, 0.0) if input_kind is EmbeddingKind.QUERY else (0.0, 1.0)
            return EmbeddingResult(tuple(vector for _ in texts), profile.model, 2)

    async def scenario():
        session = JsonlSession(tmp_path / "sessions")
        memory = MemoryService(
            store=MemoryStore(tmp_path / "memory.sqlite"),
            session=session,
            discovery=session,
            workspace="w",
            write_allowed=lambda: True,
            embedding=OrthogonalEmbedding(),
            profile=EmbeddingProfile(
                base_url="https://example.test", model="semantic", dimension=2
            ),
        )
        memory.store.publish_candidate("w", "apples", source="s:e:d")
        result = await memory.search_memory("oranges")
        assert result["method"] == "keyword+semantic" and result["results"] == []
        assert await memory.recall_part("oranges") is None

    asyncio.run(scenario())


def test_retry_reuses_durable_extraction_after_failed_merge(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    async def scenario():
        session = JsonlSession(tmp_path / "sessions")
        journal = await EventJournal.open(session, "old")
        await journal.record(
            None, RuntimeEventType.SESSION_STARTED, SessionStartedPayload(workspace_identity="w")
        )
        event = await journal.record(
            "t", RuntimeEventType.USER_MESSAGE, UserMessagePayload("use pytest")
        )
        await journal.record(
            "t", RuntimeEventType.TURN_COMPLETED, TurnCompletedPayload(StopReason.COMPLETED, 1, 0)
        )
        memory = MemoryService(
            store=MemoryStore(tmp_path / "memory.sqlite"),
            session=session,
            discovery=session,
            workspace="w",
            write_allowed=lambda: True,
        )
        job = memory.store.schedule("w", "new", 10)
        answer = {
            "candidates": [
                {"statement": "use pytest", "event_id": event.event_id, "quote": "use pytest"}
            ]
        }
        model = FakeModelPort(
            scripts=(
                (TextDelta(json.dumps(answer)), ResponseCompleted("stop")),
                (TextDelta("invalid JSON"), ResponseCompleted("stop")),
                (TextDelta('{"selected":[0]}'), ResponseCompleted("stop")),
            )
        )
        worker = MemoryMaintenance(
            memory, model, "fake", InMemoryContextObjects(), ContextBudget(64000), provider="fake"
        )
        await worker.run_pending()
        assert memory.store.job_status(job) == "pending"
        later = time.time() + 61
        monkeypatch.setattr("jdagent.memory_store.time.time", lambda: later)
        await worker.run_pending()
        assert memory.store.job_status(job) == "succeeded"
        assert len(model.requests) == 3
        assert memory.store.recall("w")[0].statement == "use pytest"

    asyncio.run(scenario())
