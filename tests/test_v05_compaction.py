import asyncio
import json
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from jdagent.adapters.fake import FakeModelPort, FakeToolRuntime
from jdagent.adapters.memory import InMemoryContextObjects, InMemorySession
from jdagent.context import ContextBuilder
from jdagent.context_archive import decode_object
from jdagent.context_budget import ContextBudget
from jdagent.context_runtime import ContextService
from jdagent.core.loop import AgentLoop
from jdagent.domain.events import (
    AssistantMessageCompletedPayload,
    CompactionCommittedPayload,
    CompactionFailedPayload,
    RuntimeEventType,
    SessionStartedPayload,
    ToolExecutionCompletedPayload,
    UserMessagePayload,
)
from jdagent.domain.model import (
    ModelCapabilities,
    ModelEvent,
    ModelRequest,
    ModelSettings,
    ResponseCompleted,
    TextDelta,
    ToolCallCompleted,
)
from jdagent.domain.tools import ToolCall, ToolExecutionContext, ToolResult, ToolResultStatus
from jdagent.eventing import EventJournal


def test_large_closed_batch_is_archived_and_compacted_without_losing_user_constraint() -> None:
    async def scenario() -> None:
        session = InMemorySession()
        objects = InMemoryContextObjects()
        journal = await EventJournal.open(session, "session")
        await journal.record(None, RuntimeEventType.SESSION_STARTED, SessionStartedPayload())
        await journal.record(
            "turn",
            RuntimeEventType.USER_MESSAGE,
            UserMessagePayload("Never edit protected.txt. Inspect the report."),
        )
        await journal.record(
            "turn",
            RuntimeEventType.ASSISTANT_MESSAGE_COMPLETED,
            AssistantMessageCompletedPayload("", (ToolCall("read", "read_file", {}),)),
        )
        await journal.record(
            "turn",
            RuntimeEventType.TOOL_EXECUTION_COMPLETED,
            ToolExecutionCompletedPayload(
                ToolResult("read", "read_file", ToolResultStatus.SUCCESS, output="x" * 3000)
            ),
        )
        summary = json.dumps(
            {
                "goal": "Inspect the report",
                "facts": [{"text": "Read the report", "status": "completed", "sources": [4]}],
                "pending": ["Explain the report"],
                "decisions": [],
                "paths": ["protected.txt"],
            }
        )
        model = FakeModelPort(
            scripts=((TextDelta(summary), ResponseCompleted("stop")),),
            repeat_last=True,
            capabilities=ModelCapabilities(context_window=2048),
        )
        builder = ContextBuilder(model="fake", settings=ModelSettings(max_output_tokens=80))
        service = ContextService(
            builder=builder,
            model=model,
            journal=journal,
            objects=objects,
            budget=ContextBudget(2048, output_tokens=80, safety_tokens=20),
        )
        request = await service.prepare_request()
        assert (
            sum(
                "Never edit protected.txt" in part.content
                for part in (*request.system_parts, *request.messages)
            )
            == 1
        )
        assert not any(message.tool_calls for message in request.messages)
        assert len(model.requests) <= 8
        assert all(not call.tools for call in model.requests)
        assert any(
            isinstance(event.payload, CompactionCommittedPayload) for event in journal.events
        )
        assert (
            service.inspect_window().estimate.input_tokens
            < service.inspect_window().budget.input_limit
        )
        assert isinstance(journal.events[3].payload, ToolExecutionCompletedPayload)
        assert journal.events[3].payload.result.output == "x" * 3000
        restored = ContextService(
            builder=builder,
            model=model,
            journal=journal,
            objects=objects,
            budget=ContextBudget(2048, output_tokens=80, safety_tokens=20),
        )
        before = len(model.requests)
        again = await restored.prepare_request()
        assert again.messages == request.messages
        assert again.system_parts == request.system_parts
        assert len(model.requests) == before

    asyncio.run(scenario())


@pytest.mark.parametrize("status", ["recorded", "completed"])
def test_statement_is_distinct_from_execution_and_failed_response_is_auditable(status: str):
    async def scenario():
        journal = await EventJournal.open(InMemorySession(), "s")
        await journal.record(None, RuntimeEventType.SESSION_STARTED, SessionStartedPayload())
        await journal.record(
            "t", RuntimeEventType.USER_MESSAGE, UserMessagePayload("Never modify protected.txt")
        )
        await journal.record(
            "t",
            RuntimeEventType.ASSISTANT_MESSAGE_COMPLETED,
            AssistantMessageCompletedPayload("archived background " * 100),
        )
        summary = json.dumps(
            {
                "goal": "Respect the constraint",
                "facts": [{"text": "User forbids edits", "status": status, "sources": [2]}],
                "pending": [],
                "decisions": [],
                "paths": ["protected.txt"],
            }
        )
        objects = InMemoryContextObjects()
        service = ContextService(
            builder=ContextBuilder(model="fake", settings=ModelSettings(max_output_tokens=1024)),
            model=FakeModelPort(scripts=((TextDelta(summary), ResponseCompleted("stop")),)),
            journal=journal,
            objects=objects,
            budget=ContextBudget(64000),
        )
        service.request_compaction()
        await service.prepare_request()
        if status == "recorded":
            assert any(isinstance(e.payload, CompactionCommittedPayload) for e in journal.events)
        else:
            failure = next(
                e.payload for e in journal.events if isinstance(e.payload, CompactionFailedPayload)
            )
            assert "promoted a plan" in failure.reason
            assert failure.response_hashes
            response = decode_object(await objects.get(failure.response_hashes[-1]))
            assert response["text"] == summary
            assert not any(
                isinstance(e.payload, CompactionCommittedPayload) for e in journal.events
            )

    asyncio.run(scenario())


def test_one_turn_continues_after_three_compactions_without_replaying_tools(tmp_path: Path) -> None:
    class BatchModel:
        capabilities = ModelCapabilities(context_window=4096)

        def __init__(self) -> None:
            self.task_requests: list[ModelRequest] = []

        async def stream(self, request: ModelRequest) -> AsyncIterator[ModelEvent]:
            if request.metadata.get("call_kind") == "compaction":
                yield TextDelta(
                    json.dumps(
                        {
                            "goal": "Inspect three reports",
                            "facts": [],
                            "pending": ["Continue remaining work"],
                            "decisions": [],
                            "paths": ["protected.txt"],
                        }
                    )
                )
                yield ResponseCompleted("stop")
                return
            self.task_requests.append(request)
            number = len(self.task_requests)
            if number <= 3:
                yield ToolCallCompleted(ToolCall(f"read-{number}", "read_file", {}))
                yield ResponseCompleted("tool_calls")
            else:
                yield TextDelta("done")
                yield ResponseCompleted("stop")

    async def scenario() -> None:
        model = BatchModel()
        journal = await EventJournal.open(InMemorySession(), "session")
        await journal.record(None, RuntimeEventType.SESSION_STARTED, SessionStartedPayload())
        await journal.record(
            "turn",
            RuntimeEventType.USER_MESSAGE,
            UserMessagePayload("Never edit protected.txt. Inspect three reports."),
        )
        tools = FakeToolRuntime(
            tuple(
                ToolResult(
                    f"read-{number}", "read_file", ToolResultStatus.SUCCESS, output="x" * 5000
                )
                for number in range(1, 4)
            )
        )
        builder = ContextBuilder(model="fake", settings=ModelSettings(max_output_tokens=200))
        service = ContextService(
            builder=builder,
            model=model,
            journal=journal,
            objects=InMemoryContextObjects(),
            budget=ContextBudget(4096, output_tokens=200, safety_tokens=20),
        )
        loop = AgentLoop(model, builder, tools, journal, context_service=service)
        result = await loop.run("turn", ToolExecutionContext("session", "turn", tmp_path))
        assert result.assistant_text == "done"
        assert result.model_calls == 4
        assert len(tools.calls) == 3
        assert (
            sum(isinstance(event.payload, CompactionCommittedPayload) for event in journal.events)
            == 3
        )
        for request in model.task_requests[1:]:
            assert "Never edit protected.txt" in "\n".join(
                part.content for part in (*request.system_parts, *request.messages)
            )

    asyncio.run(scenario())
