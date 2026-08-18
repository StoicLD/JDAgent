import asyncio
from pathlib import Path

from jdagent.adapters.fake import FakeModelPort, FakeToolRuntime
from jdagent.adapters.memory import InMemorySession
from jdagent.context import ContextBuilder
from jdagent.core.loop import AgentLoop, CancellationToken, LoopLimits
from jdagent.domain.errors import StopReason
from jdagent.domain.events import (
    RuntimeEventType,
    SessionStartedPayload,
    UserMessagePayload,
)
from jdagent.domain.model import (
    ModelErrorCategory,
    ModelFailed,
    ResponseCompleted,
    TextDelta,
    ToolCallCompleted,
)
from jdagent.domain.tools import (
    ToolCall,
    ToolExecutionContext,
    ToolResult,
    ToolResultStatus,
)
from jdagent.eventing import EventJournal


async def _journal_with_user() -> tuple[InMemorySession, EventJournal]:
    session = InMemorySession()
    journal = await EventJournal.open(session, "session-1")
    await journal.record(None, RuntimeEventType.SESSION_STARTED, SessionStartedPayload())
    await journal.record("turn-1", RuntimeEventType.USER_MESSAGE, UserMessagePayload("hello"))
    return session, journal


def test_agent_loop_completes_text_turn(tmp_path: Path) -> None:
    async def scenario() -> tuple[StopReason, EventJournal]:
        _, journal = await _journal_with_user()
        model = FakeModelPort(scripts=((TextDelta("hello"), ResponseCompleted("stop")),))
        loop = AgentLoop(
            model,
            ContextBuilder(model="fake"),
            FakeToolRuntime(()),
            journal,
        )
        result = await loop.run("turn-1", _tool_context(tmp_path))
        return result.stop_reason, journal

    stop_reason, journal = asyncio.run(scenario())

    assert stop_reason is StopReason.COMPLETED
    assert journal.events[-2].event_type is RuntimeEventType.ASSISTANT_MESSAGE_COMPLETED
    assert journal.events[-1].event_type is RuntimeEventType.TURN_COMPLETED


def test_agent_loop_completes_serial_tool_round_trip(tmp_path: Path) -> None:
    call = ToolCall("call-1", "calculator", {"expression": "2 + 3"})

    async def scenario() -> tuple[StopReason, FakeModelPort, EventJournal]:
        _, journal = await _journal_with_user()
        model = FakeModelPort(
            scripts=(
                (ToolCallCompleted(call), ResponseCompleted("tool_calls")),
                (TextDelta("The answer is 5."), ResponseCompleted("stop")),
            )
        )
        tools = FakeToolRuntime(
            (ToolResult("call-1", "calculator", ToolResultStatus.SUCCESS, output="5"),)
        )
        loop = AgentLoop(model, ContextBuilder(model="fake"), tools, journal)
        result = await loop.run("turn-1", _tool_context(tmp_path))
        return result.stop_reason, model, journal

    stop_reason, model, journal = asyncio.run(scenario())

    assert stop_reason is StopReason.COMPLETED
    assert len(model.requests) == 2
    assert model.requests[1].messages[-1].content == "5"
    assert any(
        event.event_type is RuntimeEventType.TOOL_EXECUTION_COMPLETED for event in journal.events
    )


def test_agent_loop_stops_when_cancelled(tmp_path: Path) -> None:
    async def scenario() -> tuple[StopReason, FakeModelPort]:
        _, journal = await _journal_with_user()
        model = FakeModelPort(scripts=((TextDelta("unexpected"), ResponseCompleted("stop")),))
        cancellation = CancellationToken()
        cancellation.cancel()
        loop = AgentLoop(model, ContextBuilder(model="fake"), FakeToolRuntime(()), journal)
        result = await loop.run("turn-1", _tool_context(tmp_path), cancellation=cancellation)
        return result.stop_reason, model

    stop_reason, model = asyncio.run(scenario())

    assert stop_reason is StopReason.CANCELLED
    assert model.requests == []


def test_agent_loop_classifies_model_timeout(tmp_path: Path) -> None:
    async def scenario() -> tuple[StopReason, str | None]:
        _, journal = await _journal_with_user()
        model = FakeModelPort(scripts=((ModelFailed(ModelErrorCategory.TIMEOUT, "safe timeout"),),))
        loop = AgentLoop(model, ContextBuilder(model="fake"), FakeToolRuntime(()), journal)
        result = await loop.run("turn-1", _tool_context(tmp_path))
        return result.stop_reason, result.error_category

    stop_reason, error_category = asyncio.run(scenario())

    assert stop_reason is StopReason.MODEL_ERROR
    assert error_category == "timeout"


def test_agent_loop_records_scripted_provider_error(tmp_path: Path) -> None:
    async def scenario() -> tuple[StopReason, str | None]:
        _, journal = await _journal_with_user()
        model = FakeModelPort(
            scripts=((ModelFailed(ModelErrorCategory.RATE_LIMIT, "safe retry later"),),)
        )
        loop = AgentLoop(model, ContextBuilder(model="fake"), FakeToolRuntime(()), journal)
        result = await loop.run("turn-1", _tool_context(tmp_path))
        return result.stop_reason, result.error_category

    stop_reason, error_category = asyncio.run(scenario())

    assert stop_reason is StopReason.MODEL_ERROR
    assert error_category == "rate_limit"


def test_agent_loop_stops_at_call_limit(tmp_path: Path) -> None:
    async def scenario() -> StopReason:
        _, journal = await _journal_with_user()
        call = ToolCall("call-1", "calculator", {"expression": "1"})
        model = FakeModelPort(
            scripts=((ToolCallCompleted(call), ResponseCompleted("tool_calls")),),
            repeat_last=True,
        )
        tools = FakeToolRuntime(
            (ToolResult("call-1", "calculator", ToolResultStatus.SUCCESS, output="1"),)
        )
        loop = AgentLoop(
            model,
            ContextBuilder(model="fake"),
            tools,
            journal,
            limits=LoopLimits(max_model_calls=1, max_tool_calls=2),
        )
        result = await loop.run("turn-1", _tool_context(tmp_path))
        return result.stop_reason

    assert asyncio.run(scenario()) is StopReason.LIMIT_REACHED


def test_duplicate_tool_call_ids_fail_before_any_tool_execution(tmp_path: Path) -> None:
    async def scenario() -> tuple[StopReason, int]:
        _, journal = await _journal_with_user()
        first = ToolCall("duplicate", "calculator", {"expression": "1"})
        second = ToolCall("duplicate", "calculator", {"expression": "2"})
        model = FakeModelPort(
            scripts=(
                (
                    ToolCallCompleted(first),
                    ToolCallCompleted(second),
                    ResponseCompleted("tool_calls"),
                ),
            )
        )
        tools = FakeToolRuntime(
            (
                ToolResult("duplicate", "calculator", ToolResultStatus.SUCCESS, output="1"),
                ToolResult("duplicate", "calculator", ToolResultStatus.SUCCESS, output="2"),
            )
        )
        loop = AgentLoop(model, ContextBuilder(model="fake"), tools, journal)
        result = await loop.run("turn-1", _tool_context(tmp_path))
        return result.stop_reason, len(tools.calls)

    stop_reason, execution_count = asyncio.run(scenario())

    assert stop_reason is StopReason.MODEL_ERROR
    assert execution_count == 0


def test_agent_loop_executes_multiple_calls_in_stable_order(tmp_path: Path) -> None:
    async def scenario() -> list[str]:
        _, journal = await _journal_with_user()
        first = ToolCall("call-1", "first", {})
        second = ToolCall("call-2", "second", {})
        model = FakeModelPort(
            scripts=(
                (
                    ToolCallCompleted(first),
                    ToolCallCompleted(second),
                    ResponseCompleted("tool_calls"),
                ),
                (TextDelta("done"), ResponseCompleted("stop")),
            )
        )
        tools = FakeToolRuntime(
            (
                ToolResult("call-1", "first", ToolResultStatus.SUCCESS, output="one"),
                ToolResult("call-2", "second", ToolResultStatus.SUCCESS, output="two"),
            )
        )
        loop = AgentLoop(model, ContextBuilder(model="fake"), tools, journal)
        await loop.run("turn-1", _tool_context(tmp_path))
        return [call.name for call, _ in tools.calls]

    assert asyncio.run(scenario()) == ["first", "second"]


def test_agent_loop_maps_length_finish_to_limit_reached(tmp_path: Path) -> None:
    async def scenario() -> tuple[StopReason, str | None]:
        _, journal = await _journal_with_user()
        model = FakeModelPort(scripts=((TextDelta("partial"), ResponseCompleted("length")),))
        loop = AgentLoop(model, ContextBuilder(model="fake"), FakeToolRuntime(()), journal)
        result = await loop.run("turn-1", _tool_context(tmp_path))
        return result.stop_reason, result.error_category

    stop_reason, category = asyncio.run(scenario())

    assert stop_reason is StopReason.LIMIT_REACHED
    assert category == "model_finish_length"


def test_agent_loop_rejects_tool_finish_without_tool_call(tmp_path: Path) -> None:
    async def scenario() -> tuple[StopReason, str | None]:
        _, journal = await _journal_with_user()
        model = FakeModelPort(scripts=((ResponseCompleted("tool_calls"),),))
        loop = AgentLoop(model, ContextBuilder(model="fake"), FakeToolRuntime(()), journal)
        result = await loop.run("turn-1", _tool_context(tmp_path))
        return result.stop_reason, result.error_category

    stop_reason, category = asyncio.run(scenario())

    assert stop_reason is StopReason.MODEL_ERROR
    assert category == "invalid_response"


def _tool_context(workspace: Path) -> ToolExecutionContext:
    return ToolExecutionContext("session-1", "turn-1", workspace)
