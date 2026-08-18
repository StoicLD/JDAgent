import asyncio
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from jdagent.adapters.fake import FakeApproval, FakeModelPort
from jdagent.adapters.memory import InMemorySession
from jdagent.context import ContextBuilder
from jdagent.core.loop import AgentLoop
from jdagent.domain.errors import StopReason
from jdagent.domain.events import RuntimeEventType, SessionStartedPayload, UserMessagePayload
from jdagent.domain.json import JsonObject
from jdagent.domain.model import (
    ModelCapabilities,
    ModelEvent,
    ModelRequest,
    ModelSettings,
    ResponseCompleted,
    TextDelta,
    ToolCallCompleted,
    Usage,
    UsageReported,
)
from jdagent.domain.tools import (
    ApprovalDecision,
    RiskLevel,
    ToolCall,
    ToolDefinition,
    ToolErrorCode,
    ToolExecutionContext,
)
from jdagent.eventing import EventJournal
from jdagent.observability import TraceProjection
from jdagent.tools.permissions import DefaultPermissionPolicy
from jdagent.tools.runtime import ToolRegistry, ToolRuntime
from tests.runtime_factory import create_test_coordinator


class ExplodingModel:
    @property
    def capabilities(self) -> ModelCapabilities:
        return ModelCapabilities()

    async def stream(self, request: ModelRequest) -> AsyncIterator[ModelEvent]:
        del request
        if False:
            yield ResponseCompleted("unreachable")
        raise RuntimeError("unsafe provider detail")


def test_tool_runtime_returns_timeout_result(tmp_path: Path) -> None:
    async def slow_handler(arguments: JsonObject, context: ToolExecutionContext) -> str:
        del arguments, context
        await asyncio.sleep(60)
        return "unexpected"

    tool = ToolDefinition(
        "slow_tool",
        "Wait too long.",
        {"type": "object", "additionalProperties": False},
        RiskLevel.PURE,
        slow_handler,
    )
    runtime = ToolRuntime(
        ToolRegistry((tool,)),
        DefaultPermissionPolicy(),
        FakeApproval(ApprovalDecision.APPROVE),
        timeout_seconds=0.01,
    )

    result = asyncio.run(
        runtime.execute(
            ToolCall("call-1", "slow_tool", {}),
            ToolExecutionContext("session-1", "turn-1", tmp_path),
        )
    )

    assert result.error_code is ToolErrorCode.TIMEOUT


def test_model_timeout_records_turn_failure(tmp_path: Path) -> None:
    async def scenario() -> tuple[StopReason, RuntimeEventType]:
        session = InMemorySession()
        journal = await EventJournal.open(session, "session-1")
        await journal.record(None, RuntimeEventType.SESSION_STARTED, SessionStartedPayload())
        await journal.record("turn-1", RuntimeEventType.USER_MESSAGE, UserMessagePayload("wait"))
        loop = AgentLoop(
            FakeModelPort(
                scripts=((ResponseCompleted("stop"),),),
                delay_seconds=60,
            ),
            ContextBuilder(
                model="slow",
                settings=ModelSettings(timeout_seconds=0.01),
            ),
            ToolRuntime(
                ToolRegistry(()),
                DefaultPermissionPolicy(),
                FakeApproval(ApprovalDecision.APPROVE),
            ),
            journal,
        )
        result = await loop.run(
            "turn-1",
            ToolExecutionContext("session-1", "turn-1", tmp_path),
        )
        return result.stop_reason, journal.events[-1].event_type

    stop_reason, final_event = asyncio.run(scenario())

    assert stop_reason is StopReason.MODEL_ERROR
    assert final_event is RuntimeEventType.TURN_FAILED


def test_cancellation_propagates_without_extra_side_effect(tmp_path: Path) -> None:
    target = tmp_path / "should-not-exist.txt"

    async def scenario() -> None:
        session = InMemorySession()
        journal = await EventJournal.open(session, "session-1")
        await journal.record(None, RuntimeEventType.SESSION_STARTED, SessionStartedPayload())
        await journal.record("turn-1", RuntimeEventType.USER_MESSAGE, UserMessagePayload("cancel"))
        loop = AgentLoop(
            FakeModelPort(
                scripts=((ResponseCompleted("stop"),),),
                delay_seconds=60,
            ),
            ContextBuilder(model="slow", settings=ModelSettings(timeout_seconds=10)),
            ToolRuntime(
                ToolRegistry(()),
                DefaultPermissionPolicy(),
                FakeApproval(ApprovalDecision.APPROVE),
            ),
            journal,
        )
        task = asyncio.create_task(
            loop.run(
                "turn-1",
                ToolExecutionContext("session-1", "turn-1", tmp_path),
            )
        )
        await asyncio.sleep(0)
        task.cancel()
        await task

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(scenario())
    assert not target.exists()


def test_trace_projects_same_runtime_event_ids(tmp_path: Path) -> None:
    async def scenario() -> tuple[list[str], list[str], TraceProjection]:
        session = InMemorySession()
        model = FakeModelPort(
            scripts=((UsageReported(Usage(10, 3)), TextDelta("ok"), ResponseCompleted("stop")),)
        )
        coordinator = create_test_coordinator(
            model=model,
            session=session,
            tools=(),
            approval=FakeApproval(ApprovalDecision.APPROVE),
            workspace=tmp_path,
        )
        turn = await coordinator.send("hello")
        events = [event async for event in session.read(turn.session_id)]
        return (
            [event.event_id for event in events],
            [entry.event_id for entry in turn.trace.entries],
            turn.trace,
        )

    event_ids, trace_ids, trace = asyncio.run(scenario())

    assert trace_ids == event_ids
    assert trace.summary.input_tokens == 10
    assert trace.summary.output_tokens == 3
    assert trace.summary.model_calls == 1
    assert trace.summary.stop_reason is StopReason.COMPLETED


def test_model_adapter_exception_is_classified_without_leaking_message(tmp_path: Path) -> None:
    async def scenario() -> tuple[StopReason, str | None, str]:
        session = InMemorySession()
        journal = await EventJournal.open(session, "session-1")
        await journal.record(None, RuntimeEventType.SESSION_STARTED, SessionStartedPayload())
        await journal.record("turn-1", RuntimeEventType.USER_MESSAGE, UserMessagePayload("fail"))
        loop = AgentLoop(
            ExplodingModel(),
            ContextBuilder(model="broken"),
            ToolRuntime(
                ToolRegistry(()),
                DefaultPermissionPolicy(),
                FakeApproval(ApprovalDecision.APPROVE),
            ),
            journal,
        )
        result = await loop.run(
            "turn-1",
            ToolExecutionContext("session-1", "turn-1", tmp_path),
        )
        payload = journal.events[-1].payload
        return result.stop_reason, result.error_category, str(payload)

    stop_reason, category, persisted = asyncio.run(scenario())

    assert stop_reason is StopReason.MODEL_ERROR
    assert category == "provider_internal"
    assert "unsafe provider detail" not in persisted


def test_task_cancellation_during_tool_stops_without_completion_event(tmp_path: Path) -> None:
    events_after_cancel: list[RuntimeEventType] = []

    async def scenario() -> None:
        started = asyncio.Event()

        async def slow_handler(arguments: JsonObject, context: ToolExecutionContext) -> str:
            del arguments, context
            started.set()
            await asyncio.sleep(60)
            return "unexpected"

        tool = ToolDefinition(
            "slow_tool",
            "Wait too long.",
            {"type": "object", "additionalProperties": False},
            RiskLevel.PURE,
            slow_handler,
        )
        session = InMemorySession()
        journal = await EventJournal.open(session, "session-1")
        await journal.record(None, RuntimeEventType.SESSION_STARTED, SessionStartedPayload())
        await journal.record("turn-1", RuntimeEventType.USER_MESSAGE, UserMessagePayload("run"))
        call = ToolCall("call-1", "slow_tool", {})
        model = FakeModelPort(scripts=((ToolCallCompleted(call), ResponseCompleted("tool_calls")),))
        runtime = ToolRuntime(
            ToolRegistry((tool,)),
            DefaultPermissionPolicy(),
            FakeApproval(ApprovalDecision.APPROVE),
            recorder=journal,
        )
        loop = AgentLoop(model, ContextBuilder(model="fake", tools=(tool,)), runtime, journal)
        task = asyncio.create_task(
            loop.run(
                "turn-1",
                ToolExecutionContext("session-1", "turn-1", tmp_path),
            )
        )
        await started.wait()
        task.cancel()
        try:
            await task
        finally:
            events_after_cancel.extend(event.event_type for event in journal.events)

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(scenario())

    assert events_after_cancel[-1] is RuntimeEventType.TURN_FAILED
    assert RuntimeEventType.TOOL_EXECUTION_COMPLETED not in events_after_cancel
