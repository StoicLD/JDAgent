"""Compare observable native/graph contracts and exercise graph-only failure boundaries."""

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path

import pytest

import jdagent.runtime.langgraph_runner as graph_module
from jdagent.adapters.fake import FakeModelPort, FakeToolRuntime
from jdagent.adapters.memory import InMemorySession
from jdagent.context import ContextBuilder
from jdagent.core.contracts import LoopLimits, TurnResult
from jdagent.core.loop import AgentLoop
from jdagent.domain.errors import SessionError, SessionErrorCode, StopReason
from jdagent.domain.events import (
    AssistantMessageCompletedPayload,
    RuntimeEvent,
    RuntimeEventType,
    RuntimePayload,
    SessionStartedPayload,
    ToolExecutionCompletedPayload,
    TurnFailedPayload,
    UserMessagePayload,
)
from jdagent.domain.model import (
    ModelCapabilities,
    ModelErrorCategory,
    ModelEvent,
    ModelFailed,
    ModelRequest,
    ResponseCompleted,
    TextDelta,
    ToolCallCompleted,
    ToolCallDelta,
    Usage,
    UsageReported,
)
from jdagent.domain.tools import (
    ToolCall,
    ToolErrorCode,
    ToolExecutionContext,
    ToolOutcome,
    ToolResult,
    ToolResultStatus,
)
from jdagent.eventing import EventJournal
from jdagent.knowledge.ptk import empty_prepared_knowledge
from jdagent.runtime.langgraph_runner import LangGraphTurnRunner

RunnerType = type[AgentLoop] | type[LangGraphTurnRunner]
FIRST = ToolCall("first", "read_file", {"path": "a.txt"})
SECOND = ToolCall("second", "read_file", {"path": "b.txt"})
FINAL = (TextDelta("done"), ResponseCompleted("stop"))
BAD_CITATION = (TextDelta("See K:other:E1."), ResponseCompleted("stop"))


def _batch(*calls: ToolCall, text: str = "") -> tuple[ModelEvent, ...]:
    return (
        TextDelta(text),
        *(ToolCallCompleted(call) for call in calls),
        ResponseCompleted("tool_calls"),
    )


def _result(
    call: ToolCall, error: ToolErrorCode | None = None, *, failure: bool = False
) -> ToolResult:
    return ToolResult(
        call.call_id,
        call.name,
        ToolResultStatus.ERROR if error else ToolResultStatus.SUCCESS,
        output="read",
        error_code=error,
        outcome=ToolOutcome(observed_failure=failure),
    )


@dataclass(frozen=True)
class Case:
    scripts: tuple[tuple[ModelEvent, ...], ...]
    results: tuple[ToolResult, ...] = ()
    prior_calls: tuple[ToolCall, ...] = ()
    knowledge: bool = False
    limits: LoopLimits = LoopLimits()
    stop: StopReason = StopReason.COMPLETED
    category: str | None = None
    model_calls: int = 1
    tool_calls: int = 0
    execute_calls: int = 0
    text: str = "done"


CASES = {
    "text": Case((FINAL,)),
    "late_usage": Case(
        ((TextDelta("done"), ResponseCompleted("stop"), UsageReported(Usage(8, 2))),)
    ),
    "two_tool_rounds": Case(
        (_batch(FIRST, text="read "), _batch(SECOND, text="then "), FINAL),
        (_result(FIRST), _result(SECOND)),
        model_calls=3,
        tool_calls=2,
        execute_calls=2,
        text="read then done",
    ),
    "failure_keeps_completed_prelude": Case(
        (_batch(FIRST, text="read "), (TextDelta("unfinished"),)),
        (_result(FIRST),),
        stop=StopReason.MODEL_ERROR,
        category="invalid_response",
        model_calls=2,
        tool_calls=1,
        execute_calls=1,
        text="read ",
    ),
    "batch_duplicate": Case(
        (_batch(FIRST, FIRST, text="explain"),),
        stop=StopReason.MODEL_ERROR,
        category="invalid_response",
        text="explain",
    ),
    "prior_turn_duplicate": Case(
        (_batch(FIRST),),
        prior_calls=(FIRST,),
        stop=StopReason.MODEL_ERROR,
        category="invalid_response",
        text="",
    ),
    "same_turn_duplicate": Case(
        (_batch(FIRST), _batch(FIRST)),
        (_result(FIRST),),
        stop=StopReason.MODEL_ERROR,
        category="invalid_response",
        model_calls=2,
        tool_calls=1,
        execute_calls=1,
        text="",
    ),
    "skipped_id_can_be_replanned_in_same_turn": Case(
        (_batch(FIRST, SECOND), _batch(SECOND), FINAL),
        (_result(FIRST, ToolErrorCode.INSTRUCTIONS_DISCOVERED), _result(SECOND)),
        model_calls=3,
        tool_calls=3,
        execute_calls=2,
    ),
    "discovered_rules_consume_batch_limit": Case(
        (_batch(FIRST, SECOND), _batch(SECOND)),
        (_result(FIRST, ToolErrorCode.INSTRUCTIONS_DISCOVERED),),
        limits=LoopLimits(max_tool_calls=2),
        stop=StopReason.LIMIT_REACHED,
        category="tool_call_limit",
        model_calls=2,
        tool_calls=2,
        execute_calls=1,
        text="",
    ),
    "denied_call_counts_but_does_not_stop": Case(
        (_batch(FIRST, SECOND), FINAL),
        (_result(FIRST, ToolErrorCode.PERMISSION_DENIED), _result(SECOND)),
        model_calls=2,
        tool_calls=2,
        execute_calls=2,
    ),
    "failure_budget_allows_exactly_two": Case(
        (_batch(FIRST, SECOND), FINAL),
        (_result(FIRST, failure=True), _result(SECOND, failure=True)),
        model_calls=2,
        tool_calls=2,
        execute_calls=2,
    ),
    "failure_budget_stops_third": Case(
        (_batch(FIRST, SECOND, ToolCall("third", "read_file", {})),),
        (
            _result(FIRST, failure=True),
            _result(SECOND, failure=True),
            _result(ToolCall("third", "read_file", {}), failure=True),
        ),
        stop=StopReason.LIMIT_REACHED,
        category="failure_recovery_limit",
        tool_calls=3,
        execute_calls=3,
        text="",
    ),
    "unfinished_tool_delta": Case(
        ((ToolCallDelta("partial", "read", "{"), ResponseCompleted("stop")),),
        stop=StopReason.MODEL_ERROR,
        category="invalid_response",
        text="",
    ),
    "duplicate_completion": Case(
        ((TextDelta("discard"), ResponseCompleted("stop"), ResponseCompleted("stop")),),
        stop=StopReason.MODEL_ERROR,
        category="invalid_response",
        text="",
    ),
    "incompatible_finish": Case(
        ((ToolCallCompleted(FIRST), ResponseCompleted("stop")),),
        stop=StopReason.MODEL_ERROR,
        category="invalid_response",
        text="",
    ),
    "filtered_output": Case(
        ((TextDelta("discard"), ResponseCompleted("content_filter")),),
        stop=StopReason.MODEL_ERROR,
        category="model_finish_content_filter",
        text="",
    ),
    "repair": Case((BAD_CITATION, FINAL), knowledge=True, model_calls=2),
    "repair_duplicate_completion_preserves_native_semantics": Case(
        (BAD_CITATION, (*FINAL, ResponseCompleted("stop"))),
        knowledge=True,
        model_calls=2,
    ),
    "repair_length_is_invalid_response": Case(
        (BAD_CITATION, (TextDelta("discard"), ResponseCompleted("length"))),
        knowledge=True,
        stop=StopReason.MODEL_ERROR,
        category="invalid_response",
        model_calls=2,
        text="",
    ),
    "repair_never_requests_tools": Case(
        (BAD_CITATION, _batch(FIRST)),
        knowledge=True,
        stop=StopReason.MODEL_ERROR,
        category="invalid_response",
        model_calls=2,
        text="",
    ),
    "repair_only_once": Case(
        (BAD_CITATION, BAD_CITATION),
        knowledge=True,
        stop=StopReason.MODEL_ERROR,
        category="invalid_citation",
        model_calls=2,
        text="",
    ),
    "repair_has_no_context_retry": Case(
        (BAD_CITATION, (ModelFailed(ModelErrorCategory.CONTEXT_LENGTH, "too long"),)),
        knowledge=True,
        stop=StopReason.MODEL_ERROR,
        category="context_length",
        model_calls=2,
        text="",
    ),
    "repair_respects_model_limit": Case(
        (BAD_CITATION,),
        knowledge=True,
        limits=LoopLimits(max_model_calls=1),
        stop=StopReason.LIMIT_REACHED,
        category="model_call_limit",
        text="",
    ),
}


@dataclass
class Observed:
    result: TurnResult
    requests: list[ModelRequest]
    events: list[tuple[RuntimeEventType, RuntimePayload]]
    calls: list[ToolCall]
    displayed: list[ModelEvent]


class Display:
    def __init__(self) -> None:
        self.events: list[ModelEvent] = []

    async def observe(self, event: ModelEvent) -> None:
        self.events.append(event)


async def _journal() -> EventJournal:
    journal = await EventJournal.open(InMemorySession(), "session")
    await journal.record(None, RuntimeEventType.SESSION_STARTED, SessionStartedPayload())
    return journal


async def _run(case: Case, runner_type: RunnerType, workspace: Path) -> Observed:
    await asyncio.to_thread(workspace.mkdir)
    journal = await _journal()
    if case.prior_calls:
        await journal.record(
            "previous", RuntimeEventType.USER_MESSAGE, UserMessagePayload("earlier")
        )
        await journal.record(
            "previous",
            RuntimeEventType.ASSISTANT_MESSAGE_COMPLETED,
            AssistantMessageCompletedPayload("", case.prior_calls),
        )
        for call in case.prior_calls:
            await journal.record(
                "previous",
                RuntimeEventType.TOOL_EXECUTION_COMPLETED,
                ToolExecutionCompletedPayload(_result(call)),
            )
    await journal.record("turn", RuntimeEventType.USER_MESSAGE, UserMessagePayload("hello"))
    model = FakeModelPort(scripts=case.scripts)
    tools = FakeToolRuntime(case.results)
    display = Display()
    runner = runner_type(
        model,
        ContextBuilder(model="fake"),
        tools,
        journal,
        limits=case.limits,
        model_event_observers=(display,),
    )
    result = await runner.run(
        "turn",
        ToolExecutionContext("session", "turn", workspace),
        knowledge=empty_prepared_knowledge("turn", "tok", "query") if case.knowledge else None,
    )
    assert result.stop_reason is case.stop
    assert result.error_category == case.category
    assert (result.model_calls, result.tool_calls, len(tools.calls)) == (
        case.model_calls,
        case.tool_calls,
        case.execute_calls,
    )
    assert result.assistant_text == case.text
    assert (
        sum(
            e.event_type in {RuntimeEventType.TURN_COMPLETED, RuntimeEventType.TURN_FAILED}
            for e in journal.events
        )
        == 1
    )
    return Observed(
        result,
        model.requests,
        [(e.event_type, e.payload) for e in journal.events],
        [call for call, _ in tools.calls],
        display.events,
    )


@pytest.mark.parametrize("name", CASES)
def test_graph_matches_native_requests_events_results_and_tool_order(
    tmp_path: Path, name: str
) -> None:
    async def scenario() -> None:
        case = CASES[name]
        native = await _run(case, AgentLoop, tmp_path / "native")
        graph = await _run(case, LangGraphTurnRunner, tmp_path / "graph")
        assert graph == native
        if case.knowledge and len(graph.requests) == 2:
            assert not graph.requests[1].tools
            assert len(graph.displayed) == len(case.scripts[0])
            assert all(
                "K:other:E1" not in message.content for message in graph.requests[1].messages
            )

    asyncio.run(scenario())


def test_graph_step_limit_allows_full_business_budget(tmp_path: Path) -> None:
    calls = tuple(ToolCall(f"call-{i}", "read_file", {}) for i in range(32))
    case = Case(
        tuple(_batch(*calls[i : i + 2]) for i in range(0, 32, 2)),
        tuple(_result(call) for call in calls),
        stop=StopReason.LIMIT_REACHED,
        category="model_call_limit",
        model_calls=16,
        tool_calls=32,
        execute_calls=32,
        text="",
    )

    async def scenario() -> None:
        assert await _run(case, LangGraphTurnRunner, tmp_path / "graph") == await _run(
            case, AgentLoop, tmp_path / "native"
        )

    asyncio.run(scenario())


def test_graph_step_guard_records_one_failure_without_running_a_tool(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def forced_limit(limits: LoopLimits) -> int:
        del limits
        return 2

    monkeypatch.setattr(graph_module, "_step_limit", forced_limit)

    async def scenario() -> None:
        case = Case(
            (_batch(FIRST),), stop=StopReason.INTERNAL_ERROR, category="graph_step_limit", text=""
        )
        await _run(case, LangGraphTurnRunner, tmp_path / "graph")

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "terminal", [RuntimeEventType.TURN_COMPLETED, RuntimeEventType.TURN_FAILED]
)
def test_exception_after_terminal_persistence_does_not_append_another_terminal(
    tmp_path: Path, terminal: RuntimeEventType
) -> None:
    class Observer:
        async def observe(self, event: RuntimeEvent) -> None:
            if event.event_type is terminal:
                raise RuntimeError("observer failure after persistence")

    async def scenario() -> None:
        journal = await EventJournal.open(InMemorySession(), "session", observers=(Observer(),))
        await journal.record("turn", RuntimeEventType.USER_MESSAGE, UserMessagePayload("hello"))
        script = (
            FINAL
            if terminal is RuntimeEventType.TURN_COMPLETED
            else (ModelFailed(ModelErrorCategory.TIMEOUT, "timeout"),)
        )
        model = FakeModelPort(scripts=(script,))
        runner = LangGraphTurnRunner(
            model, ContextBuilder(model="fake"), FakeToolRuntime(()), journal
        )
        result = await runner.run("turn", ToolExecutionContext("session", "turn", tmp_path))
        assert result.stop_reason is (
            StopReason.COMPLETED
            if terminal is RuntimeEventType.TURN_COMPLETED
            else StopReason.MODEL_ERROR
        )
        assert [
            e.event_type
            for e in journal.events
            if e.event_type in {RuntimeEventType.TURN_COMPLETED, RuntimeEventType.TURN_FAILED}
        ] == [terminal]

    asyncio.run(scenario())


def test_session_append_failure_propagates_without_claiming_a_saved_turn_failure(
    tmp_path: Path,
) -> None:
    class FailingSession(InMemorySession):
        async def append(self, event: RuntimeEvent) -> None:
            if event.event_type is RuntimeEventType.ASSISTANT_MESSAGE_COMPLETED:
                raise SessionError(SessionErrorCode.APPEND_FAILED, "disk unavailable")
            await super().append(event)

    async def scenario() -> None:
        journal = await EventJournal.open(FailingSession(), "session")
        await journal.record("turn", RuntimeEventType.USER_MESSAGE, UserMessagePayload("hello"))
        model = FakeModelPort(scripts=(FINAL,))
        runner = LangGraphTurnRunner(
            model, ContextBuilder(model="fake"), FakeToolRuntime(()), journal
        )
        with pytest.raises(SessionError, match="disk unavailable"):
            await runner.run("turn", ToolExecutionContext("session", "turn", tmp_path))
        assert len(journal.events) == 1

    asyncio.run(scenario())


def test_model_task_cancellation_waits_for_stream_close_and_records_one_failure(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        started, closed = asyncio.Event(), asyncio.Event()

        class WaitingModel:
            @property
            def capabilities(self) -> ModelCapabilities:
                return ModelCapabilities()

            async def stream(self, request: ModelRequest) -> AsyncIterator[ModelEvent]:
                del request
                try:
                    started.set()
                    yield TextDelta("incomplete")
                    await asyncio.Event().wait()
                finally:
                    closed.set()

        journal = await _journal()
        await journal.record("turn", RuntimeEventType.USER_MESSAGE, UserMessagePayload("wait"))
        runner = LangGraphTurnRunner(
            WaitingModel(), ContextBuilder(model="fake"), FakeToolRuntime(()), journal
        )
        task = asyncio.create_task(
            runner.run("turn", ToolExecutionContext("session", "turn", tmp_path))
        )
        await asyncio.wait_for(started.wait(), timeout=2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert closed.is_set()
        failures = [e.payload for e in journal.events if isinstance(e.payload, TurnFailedPayload)]
        assert len(failures) == 1 and failures[0].model_calls == 1
        assert failures[0].stop_reason is StopReason.CANCELLED
        assert not any(
            isinstance(e.payload, AssistantMessageCompletedPayload) for e in journal.events
        )

    asyncio.run(scenario())


@pytest.mark.parametrize("runner_type", [AgentLoop, LangGraphTurnRunner])
def test_skipped_id_is_rejected_in_next_turn_and_run_counters_reset(
    tmp_path: Path, runner_type: RunnerType
) -> None:
    async def scenario() -> None:
        journal = await _journal()
        model = FakeModelPort(scripts=(_batch(FIRST, SECOND), FINAL, _batch(SECOND)))
        tools = FakeToolRuntime((_result(FIRST, ToolErrorCode.INSTRUCTIONS_DISCOVERED),))
        runner = runner_type(model, ContextBuilder(model="fake"), tools, journal)
        await journal.record("one", RuntimeEventType.USER_MESSAGE, UserMessagePayload("first"))
        first = await runner.run("one", ToolExecutionContext("session", "one", tmp_path))
        assert first.stop_reason is StopReason.COMPLETED
        assert (first.model_calls, first.tool_calls) == (2, 2)
        await journal.record("two", RuntimeEventType.USER_MESSAGE, UserMessagePayload("next"))
        second = await runner.run("two", ToolExecutionContext("session", "two", tmp_path))
        assert second.error_category == "invalid_response"
        assert (second.model_calls, second.tool_calls) == (1, 0)
        assert second.assistant_text == ""
        assert len(tools.calls) == 1
        assert not any(
            e.turn_id == "two" and isinstance(e.payload, AssistantMessageCompletedPayload)
            for e in journal.events
        )

    asyncio.run(scenario())


def test_two_graph_sessions_keep_contexts_results_and_tools_isolated(tmp_path: Path) -> None:
    async def scenario() -> None:
        arrived = 0
        both_started = asyncio.Event()

        class InterleavedModel(FakeModelPort):
            async def stream(self, request: ModelRequest) -> AsyncIterator[ModelEvent]:
                nonlocal arrived
                if not self.requests:
                    arrived += 1
                    if arrived == 2:
                        both_started.set()
                    await asyncio.wait_for(both_started.wait(), timeout=2)
                async for event in super().stream(request):
                    yield event

        async def session(name: str, denied: bool) -> None:
            journal = await EventJournal.open(InMemorySession(), name)
            await journal.record("turn", RuntimeEventType.USER_MESSAGE, UserMessagePayload(name))
            model = InterleavedModel(
                scripts=(_batch(FIRST), (TextDelta(name), ResponseCompleted("stop")))
            )
            tools = FakeToolRuntime(
                (_result(FIRST, ToolErrorCode.PERMISSION_DENIED if denied else None),)
            )
            runner = LangGraphTurnRunner(model, ContextBuilder(model=name), tools, journal)
            result = await runner.run("turn", ToolExecutionContext(name, "turn", tmp_path / name))
            assert result.assistant_text == name
            assert (result.model_calls, result.tool_calls) == (2, 1)
            assert all(request.model == name for request in model.requests)
            assert all(event.session_id == name for event in journal.events)
            assert [context.session_id for _, context in tools.calls] == [name]
            persisted = [
                e.payload.result
                for e in journal.events
                if isinstance(e.payload, ToolExecutionCompletedPayload)
            ]
            assert persisted[0].error_code is (ToolErrorCode.PERMISSION_DENIED if denied else None)

        await asyncio.gather(session("alpha", False), session("beta", True))

    asyncio.run(scenario())


def test_graph_does_not_retry_tool_exception(tmp_path: Path) -> None:
    class ExplodingTools:
        calls = 0

        async def execute(self, call: ToolCall, context: ToolExecutionContext) -> ToolResult:
            del call, context
            self.calls += 1
            raise RuntimeError("private tool details")

    async def scenario() -> None:
        journal = await _journal()
        await journal.record("turn", RuntimeEventType.USER_MESSAGE, UserMessagePayload("execute"))
        tools = ExplodingTools()
        model = FakeModelPort(scripts=(_batch(FIRST),))
        runner = LangGraphTurnRunner(model, ContextBuilder(model="fake"), tools, journal)
        result = await runner.run("turn", ToolExecutionContext("session", "turn", tmp_path))
        assert tools.calls == 1 and len(model.requests) == 1
        assert result.stop_reason is StopReason.INTERNAL_ERROR
        assert result.tool_calls == 0
        assert journal.events[-2].event_type is RuntimeEventType.TOOL_EXECUTION_COMPLETED
        failure = journal.events[-1].payload
        assert isinstance(failure, TurnFailedPayload)
        assert failure.message == "Turn engine failed: RuntimeError"

    asyncio.run(scenario())
