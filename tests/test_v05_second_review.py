import asyncio
import json
from collections.abc import AsyncIterator
from pathlib import Path

from jdagent.adapters.fake import FakeModelPort
from jdagent.adapters.jsonl_session import JsonlSession
from jdagent.adapters.memory import InMemoryContextObjects, InMemorySession
from jdagent.context import ContextBuilder
from jdagent.context_archive import decode_object
from jdagent.context_budget import ContextBudget
from jdagent.context_runtime import ContextService
from jdagent.domain.errors import StopReason
from jdagent.domain.events import (
    AssistantMessageCompletedPayload,
    CompactionCommittedPayload,
    RuntimeEventType,
    SessionStartedPayload,
    ToolExecutionCompletedPayload,
    TurnCompletedPayload,
    UserMessagePayload,
)
from jdagent.domain.model import (
    ModelCapabilities,
    ModelEvent,
    ModelRequest,
    ModelSettings,
    ResponseCompleted,
    TextDelta,
)
from jdagent.domain.tools import ToolCall, ToolResult, ToolResultStatus
from jdagent.eventing import EventJournal
from jdagent.maintenance import MemoryMaintenance
from jdagent.memory_service import MemoryService
from jdagent.memory_store import MemoryStore


def test_busy_publication_reuses_merge_and_disabling_prevents_publication(tmp_path: Path):
    async def scenario():
        session = JsonlSession(tmp_path / "sessions")
        journal = await EventJournal.open(session, "old")
        await journal.record(
            None, RuntimeEventType.SESSION_STARTED, SessionStartedPayload(workspace_identity="w")
        )
        user = await journal.record(
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
        allowed = True

        class RacingModel:
            capabilities = ModelCapabilities(context_window=64000)
            requests = 0

            async def stream(self, request: ModelRequest) -> AsyncIterator[ModelEvent]:
                self.requests += 1
                if self.requests == 1:
                    yield TextDelta(
                        json.dumps(
                            {
                                "candidates": [
                                    {
                                        "statement": "project uses pytest",
                                        "quote": "project uses pytest",
                                        "event_id": user.event_id,
                                    }
                                ]
                            }
                        )
                    )
                else:
                    memory.active_sessions.add("old")
                    yield TextDelta('{"selected":[0]}')
                yield ResponseCompleted("stop")

        model = RacingModel()
        job = memory.store.schedule("w", "new", 1)
        worker = MemoryMaintenance(
            memory,
            model,
            "fake",
            InMemoryContextObjects(),
            ContextBudget(64000),
            provider="fake",
            automatic_allowed=lambda: allowed,
        )
        await worker.run_pending()
        assert memory.store.job_status(job) == "pending" and model.requests == 2
        memory.active_sessions.clear()
        allowed = False
        await worker.run_pending()
        assert memory.store.job_status(job) == "pending" and not memory.store.recall("w")
        allowed = True
        await worker.run_pending()
        assert memory.store.job_status(job) == "succeeded" and model.requests == 2
        assert len(memory.store.recall("w")) == 1

    asyncio.run(scenario())


def test_twenty_compactions_do_not_reload_all_prior_segments():
    async def scenario():
        journal = await EventJournal.open(InMemorySession(), "s")
        await journal.record(None, RuntimeEventType.SESSION_STARTED, SessionStartedPayload())
        await journal.record(
            "t",
            RuntimeEventType.USER_MESSAGE,
            UserMessagePayload("Never edit protected.txt. Read reports."),
        )
        summary = json.dumps(
            {
                "goal": "Read reports",
                "facts": [],
                "pending": ["Continue"],
                "decisions": [],
                "paths": [],
            }
        )
        model = FakeModelPort(
            scripts=((TextDelta(summary), ResponseCompleted("stop")),),
            repeat_last=True,
            capabilities=ModelCapabilities(context_window=4096),
        )
        service = ContextService(
            builder=ContextBuilder(model="fake", settings=ModelSettings(max_output_tokens=200)),
            model=model,
            journal=journal,
            objects=InMemoryContextObjects(),
            budget=ContextBudget(4096, output_tokens=200, safety_tokens=20),
        )
        for index in range(20):
            await journal.record(
                "t",
                RuntimeEventType.ASSISTANT_MESSAGE_COMPLETED,
                AssistantMessageCompletedPayload("", (ToolCall(str(index), "read_file", {}),)),
            )
            await journal.record(
                "t",
                RuntimeEventType.TOOL_EXECUTION_COMPLETED,
                ToolExecutionCompletedPayload(
                    ToolResult(str(index), "read_file", ToolResultStatus.SUCCESS, output="x" * 5000)
                ),
            )
            request = await service.prepare_request()
            assert (
                sum(
                    "Never edit protected.txt" in part.content
                    for part in (*request.system_parts, *request.messages)
                )
                == 1
            )
        assert (
            sum(isinstance(event.payload, CompactionCommittedPayload) for event in journal.events)
            == 20
        )
        assert max(len(request.messages[0].content.encode()) for request in model.requests) < 4096
        for maintenance_request in model.requests:
            index = next(
                part
                for part in maintenance_request.system_parts
                if part.source == "maintenance_sources"
            )
            assert len(json.loads(index.content.split(": ", 1)[1])) <= 1

    asyncio.run(scenario())


def test_original_segment_restores_fact_omitted_by_intermediate_merge():
    async def scenario():
        journal = await EventJournal.open(InMemorySession(), "s")
        await journal.record(None, RuntimeEventType.SESSION_STARTED, SessionStartedPayload())
        await journal.record(
            "t", RuntimeEventType.USER_MESSAGE, UserMessagePayload("Inspect reports")
        )
        objects = InMemoryContextObjects()

        class LossyMergeModel:
            capabilities = ModelCapabilities(context_window=4096)
            phase = 0
            recovered_source = False

            async def stream(self, request: ModelRequest) -> AsyncIterator[ModelEvent]:
                content = request.messages[0].content
                retained = self.phase == 0 or (
                    self.phase == 2 and "EARLY_FACT: port 7123" in content
                )
                if self.phase == 2 and "EARLY_FACT: port 7123" in content:
                    self.recovered_source = True
                yield TextDelta(
                    json.dumps(
                        {
                            "goal": "Inspect reports",
                            "facts": [
                                {
                                    "text": "EARLY_FACT: port 7123",
                                    "status": "completed",
                                    "sources": [4],
                                }
                            ]
                            if retained
                            else [],
                            "pending": [],
                            "decisions": [],
                            "paths": [],
                        }
                    )
                )
                yield ResponseCompleted("stop")

        model = LossyMergeModel()
        service = ContextService(
            builder=ContextBuilder(model="fake", settings=ModelSettings(max_output_tokens=200)),
            model=model,
            journal=journal,
            objects=objects,
            budget=ContextBudget(4096, output_tokens=200, safety_tokens=20),
        )
        for phase in range(3):
            model.phase = phase
            if phase == 2:
                await journal.record(
                    "t", RuntimeEventType.USER_MESSAGE, UserMessagePayload("Retrieve EARLY_FACT")
                )
            await journal.record(
                "t",
                RuntimeEventType.ASSISTANT_MESSAGE_COMPLETED,
                AssistantMessageCompletedPayload("", (ToolCall(str(phase), "read_file", {}),)),
            )
            await journal.record(
                "t",
                RuntimeEventType.TOOL_EXECUTION_COMPLETED,
                ToolExecutionCompletedPayload(
                    ToolResult(
                        str(phase),
                        "read_file",
                        ToolResultStatus.SUCCESS,
                        output=("EARLY_FACT: port 7123\n" if phase == 0 else "") + "x" * 5000,
                    )
                ),
            )
            await service.prepare_request()
            commit = next(
                event.payload
                for event in reversed(journal.events)
                if isinstance(event.payload, CompactionCommittedPayload)
            )
            checkpoint = decode_object(await objects.get(commit.object_hash))
            if phase == 1:
                assert "EARLY_FACT" not in json.dumps(checkpoint["summary"])
            if phase == 2:
                assert model.recovered_source
                assert "EARLY_FACT" in json.dumps(checkpoint["summary"])

    asyncio.run(scenario())
