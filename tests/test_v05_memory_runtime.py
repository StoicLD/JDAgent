import asyncio
import json
from pathlib import Path

from jdagent.adapters.fake import FakeApproval, FakeModelPort
from jdagent.adapters.jsonl_session import JsonlSession
from jdagent.adapters.memory import InMemoryContextObjects
from jdagent.context_budget import ContextBudget
from jdagent.domain.errors import StopReason
from jdagent.domain.events import (
    RuntimeEventType,
    SessionStartedPayload,
    TurnCompletedPayload,
    UserMessagePayload,
)
from jdagent.domain.model import ResponseCompleted, TextDelta
from jdagent.domain.tools import ApprovalDecision, ToolCall, ToolExecutionContext, ToolResultStatus
from jdagent.eventing import EventJournal
from jdagent.maintenance import MemoryMaintenance
from jdagent.memory_service import MemoryService
from jdagent.memory_store import MemoryStore
from jdagent.tools.permissions import DefaultPermissionPolicy
from jdagent.tools.runtime import ToolRegistry, ToolRuntime


def test_direct_memory_write_uses_bound_intent_without_second_approval(tmp_path: Path):
    async def scenario():
        session = JsonlSession(tmp_path / "sessions")
        memory = MemoryService(
            store=MemoryStore(tmp_path / "memory.sqlite"),
            session=session,
            discovery=session,
            workspace="w",
            write_allowed=lambda: True,
        )
        intent = memory.store.accept_intent(
            "记住：使用 pytest", workspace="w", session="s", turn="t", event="e"
        )
        assert intent
        arguments = memory.store.intent_arguments(intent)
        runtime = ToolRuntime(
            ToolRegistry(memory.tools()),
            DefaultPermissionPolicy(),
            FakeApproval(ApprovalDecision.REJECT),
        )
        result = await runtime.execute(
            ToolCall("call", "update_memory", arguments),
            ToolExecutionContext("s", "t", tmp_path, memory_intent=intent),
        )
        assert result.status is ToolResultStatus.SUCCESS
        forged = await runtime.execute(
            ToolCall("forged", "update_memory", {**arguments, "statement": "forged"}),
            ToolExecutionContext("s", "t", tmp_path, memory_intent=intent),
        )
        assert forged.status is ToolResultStatus.ERROR
        assert memory.store.recall("w")[0].statement == "使用 pytest"

    asyncio.run(scenario())


def test_durable_two_stage_job_publishes_only_verified_other_source(tmp_path: Path):
    async def scenario():
        session = JsonlSession(tmp_path / "sessions")
        journal = await EventJournal.open(session, "old")
        await journal.record(
            None, RuntimeEventType.SESSION_STARTED, SessionStartedPayload(workspace_identity="w")
        )
        event = await journal.record(
            "t", RuntimeEventType.USER_MESSAGE, UserMessagePayload("project uses pytest")
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
        candidate = {
            "candidates": [
                {
                    "statement": "project uses pytest",
                    "event_id": event.event_id,
                    "quote": "project uses pytest",
                }
            ]
        }
        model = FakeModelPort(
            scripts=(
                (TextDelta(json.dumps(candidate)), ResponseCompleted("stop")),
                (TextDelta('{"selected":[0]}'), ResponseCompleted("stop")),
            )
        )
        worker = MemoryMaintenance(
            memory, model, "fake", InMemoryContextObjects(), ContextBudget(64000), provider="fake"
        )
        await worker.run_pending()
        assert memory.store.job_status(job) == "succeeded"
        assert memory.store.recall("w")[0].statement == "project uses pytest"
        assert len(model.requests) == 2 and all(not request.tools for request in model.requests)
        result = await memory.search_history("pytest")
        assert result["total"] == 1
        await worker.run_pending()
        assert len(model.requests) == 2

    asyncio.run(scenario())


def test_more_than_six_sources_finish_in_bounded_continuation_jobs(tmp_path: Path):
    async def scenario():
        session = JsonlSession(tmp_path / "sessions")
        scripts: list[tuple[TextDelta, ResponseCompleted]] = []
        for index in range(7):
            journal = await EventJournal.open(session, f"source-{index}")
            await journal.record(
                None,
                RuntimeEventType.SESSION_STARTED,
                SessionStartedPayload(workspace_identity="w"),
            )
            statement = f"component {index} uses pytest"
            user = await journal.record(
                "t", RuntimeEventType.USER_MESSAGE, UserMessagePayload(statement)
            )
            await journal.record(
                "t",
                RuntimeEventType.TURN_COMPLETED,
                TurnCompletedPayload(StopReason.COMPLETED, 1, 0),
            )
            scripts.append(
                (
                    TextDelta(
                        json.dumps(
                            {
                                "candidates": [
                                    {
                                        "statement": statement,
                                        "event_id": user.event_id,
                                        "quote": statement,
                                    }
                                ]
                            }
                        )
                    ),
                    ResponseCompleted("stop"),
                )
            )
        scripts.insert(6, (TextDelta('{"selected":[0,1,2,3,4,5]}'), ResponseCompleted("stop")))
        scripts.append((TextDelta('{"selected":[0]}'), ResponseCompleted("stop")))
        memory = MemoryService(
            store=MemoryStore(tmp_path / "memory.sqlite"),
            session=session,
            discovery=session,
            workspace="w",
            write_allowed=lambda: True,
        )
        job = memory.store.schedule("w", "current", 1)
        model = FakeModelPort(scripts=tuple(scripts))
        worker = MemoryMaintenance(
            memory, model, "fake", InMemoryContextObjects(), ContextBudget(64000), provider="fake"
        )
        await worker.run_pending()
        assert memory.store.job_status(job) == "succeeded"
        assert {item.statement for item in memory.store.recall("w")} == {
            f"component {index} uses pytest" for index in range(7)
        }
        assert len(model.requests) == 9
        jobs = [request.metadata["job_id"] for request in model.requests]
        assert len(set(jobs)) == 2
        assert max(jobs.count(identity) for identity in jobs) <= 8
        await worker.run_pending()
        assert len(model.requests) == 9

    asyncio.run(scenario())
