"""Sequential StateGraph execution; the journal remains the only durable history."""

import asyncio
from collections.abc import Awaitable
from dataclasses import dataclass
from typing import Literal, Protocol, TypedDict

from langgraph.constants import END, START
from langgraph.errors import GraphRecursionError
from langgraph.graph.state import StateGraph  # pyright: ignore[reportMissingTypeStubs]
from langsmith import tracing_context  # pyright: ignore[reportUnknownVariableType]

from jdagent.context import ContextBuilder, ContextLimitError
from jdagent.context_runtime import CompactionError, ContextService
from jdagent.core.contracts import CancellationToken, LoopLimits, ModelEventObserver, TurnResult
from jdagent.domain.errors import SessionError, StopReason
from jdagent.domain.events import (
    AssistantMessageCompletedPayload,
    CitationRecord,
    ContextWindowSelectedPayload,
    ModelUsageRecordedPayload,
    RuntimeEventType,
    ToolCallRequestedPayload,
    ToolExecutionCompletedPayload,
    TurnCompletedPayload,
    TurnFailedPayload,
)
from jdagent.domain.model import (
    ModelErrorCategory,
    ModelRequest,
    ResponseCompleted,
    TextDelta,
    ToolCallCompleted,
    ToolCallDelta,
    UsageReported,
)
from jdagent.domain.tools import ToolCall, ToolErrorCode, ToolExecutionContext
from jdagent.eventing import close_pending_calls
from jdagent.knowledge.citation import finalize_answer, repair_message
from jdagent.knowledge.ptk import PreparedTurnKnowledge
from jdagent.model_stream import closing_model_stream
from jdagent.ports import ModelPort, RuntimeJournal, ToolRuntimePort


@dataclass(frozen=True, slots=True)
class _Failure:
    reason: StopReason
    category: str
    message: str


class _TurnState(TypedDict):
    session_id: str
    turn_id: str
    model_calls: int
    tool_calls: int
    observed_failures: int
    seen_call_ids: set[str]
    call_kind: Literal["task", "citation_repair"]
    repair_attempted: bool
    context_retried: bool
    request: ModelRequest | None
    response_text: str
    pending_calls: tuple[ToolCall, ...]
    tool_index: int
    turn_text_parts: list[str]
    citations: tuple[CitationRecord, ...]
    model_supplement: str
    next_node: str
    failure: _Failure | None
    result: TurnResult | None


@dataclass(frozen=True, slots=True)
class _Dependencies:
    model: ModelPort
    builder: ContextBuilder
    tools: ToolRuntimePort
    journal: RuntimeJournal
    limits: LoopLimits
    provider_name: str
    model_name: str
    observers: tuple[ModelEventObserver, ...]
    context: ContextService | None


class _Node(Protocol):
    def __call__(self, state: _TurnState) -> Awaitable[_TurnState]: ...


def _step_limit(limits: LoopLimits) -> int:
    # Each model attempt visits at most six nodes; each executed tool adds one.
    # Leave room for the tool-limit guard, terminal node, and citation repair guards.
    return 6 * limits.max_model_calls + 2 * limits.max_tool_calls + 10


def _route(state: _TurnState) -> str:
    return state["next_node"]


class LangGraphTurnRunner:
    """Run project services through a fresh graph invocation without checkpoint replay.

    Dependencies belong to this session. Per-turn mutable state and bound node methods
    are created inside run(), so reusing a runner does not reuse counters or candidates.
    """

    def __init__(
        self,
        model: ModelPort,
        context_builder: ContextBuilder,
        tools: ToolRuntimePort,
        journal: RuntimeJournal,
        *,
        limits: LoopLimits | None = None,
        provider_name: str = "unknown",
        model_name: str = "unknown",
        model_event_observers: tuple[ModelEventObserver, ...] = (),
        context_service: ContextService | None = None,
    ) -> None:
        self._dependencies = _Dependencies(
            model,
            context_builder,
            tools,
            journal,
            limits or LoopLimits(),
            provider_name,
            model_name,
            model_event_observers,
            context_service,
        )

    async def run(
        self,
        turn_id: str,
        tool_context: ToolExecutionContext,
        *,
        cancellation: CancellationToken | None = None,
        knowledge: PreparedTurnKnowledge | None = None,
    ) -> TurnResult:
        execution = _Execution(
            self._dependencies,
            turn_id,
            tool_context,
            cancellation or CancellationToken(),
            knowledge,
        )
        return await execution.run()


class _Execution:
    def __init__(
        self,
        dependencies: _Dependencies,
        turn_id: str,
        tool_context: ToolExecutionContext,
        cancellation: CancellationToken,
        knowledge: PreparedTurnKnowledge | None,
    ) -> None:
        self.d = dependencies
        self.tool_context = tool_context
        self.cancellation = cancellation
        self.knowledge = knowledge
        self.stage = "prepare"
        self.finishing = False
        self.state: _TurnState = {
            "session_id": tool_context.session_id,
            "turn_id": turn_id,
            "model_calls": 0,
            "tool_calls": 0,
            "observed_failures": 0,
            "seen_call_ids": {
                call.call_id
                for event in dependencies.journal.events
                if isinstance(event.payload, AssistantMessageCompletedPayload)
                for call in event.payload.tool_calls
            },
            "call_kind": "task",
            "repair_attempted": False,
            "context_retried": False,
            "request": None,
            "response_text": "",
            "pending_calls": (),
            "tool_index": 0,
            "turn_text_parts": [],
            "citations": (),
            "model_supplement": "",
            "next_node": "prepare",
            "failure": None,
            "result": None,
        }

    def _tracked(self, name: str, node: _Node) -> _Node:
        async def execute(state: _TurnState) -> _TurnState:
            # Retain progress made before an await that raises. There is one writer,
            # no parallel nodes, no checkpoint, and no reducer that could replay it.
            self.stage, self.state = name, state
            return await node(state)

        return execute

    async def run(self) -> TurnResult:
        graph = StateGraph(_TurnState)
        nodes: dict[str, tuple[_Node, list[str]]] = {
            "prepare": (self.prepare, ["model", "fail"]),
            "model": (self.model, ["prepare", "citations", "fail"]),
            "citations": (self.citations, ["repair_prepare", "persist", "fail"]),
            "repair_prepare": (self.repair_prepare, ["repair_model", "fail"]),
            "repair_model": (self.repair_model, ["citations", "fail"]),
            "persist": (self.persist, ["tool", "complete", "fail"]),
            "tool": (self.tool, ["tool", "prepare", "fail"]),
            "complete": (self.complete, []),
            "fail": (self.fail, []),
        }
        for name, (node, destinations) in nodes.items():
            graph.add_node(name, self._tracked(name, node))  # pyright: ignore[reportUnknownMemberType]
            if destinations:
                graph.add_conditional_edges(name, _route, {dest: dest for dest in destinations})
            else:
                graph.add_edge(name, END)
        graph.add_edge(START, "prepare")
        compiled = graph.compile(checkpointer=False)  # pyright: ignore[reportUnknownMemberType]
        try:
            with tracing_context(enabled=False):
                await compiled.ainvoke(  # pyright: ignore[reportUnknownMemberType]
                    self.state,
                    config={"recursion_limit": _step_limit(self.d.limits), "callbacks": []},
                )
        except asyncio.CancelledError:
            if self._terminal_result() is None and not self.finishing:
                message = {
                    "tool": "Turn task was cancelled during tool execution",
                    "repair_model": "Citation repair cancelled",
                }.get(self.stage, "Turn task was cancelled")
                self._failure(self.state, StopReason.CANCELLED, "cancelled", message)
                await self.fail(self.state)
            raise
        except SessionError:
            # Failed persistence is not evidence of a persisted failure event.
            raise
        except Exception as error:
            terminal = self._terminal_result()
            if terminal is not None:
                return terminal
            if self.finishing:
                raise
            category = (
                "graph_step_limit" if isinstance(error, GraphRecursionError) else "internal_error"
            )
            self._failure(
                self.state,
                StopReason.INTERNAL_ERROR,
                category,
                f"Turn engine failed: {type(error).__name__}",
            )
            await self.fail(self.state)
        result = self.state["result"]
        assert result is not None
        return result

    def _failure(
        self, state: _TurnState, reason: StopReason, category: str, message: str
    ) -> _TurnState:
        state["failure"] = _Failure(reason, category, message)
        state["next_node"] = "fail"
        return state

    def _invalid(self, state: _TurnState, message: str) -> _TurnState:
        return self._failure(state, StopReason.MODEL_ERROR, "invalid_response", message)

    async def _prepare(self, state: _TurnState, *, repair: bool) -> _TurnState:
        if self.cancellation.cancelled or state["model_calls"] >= self.d.limits.max_model_calls:
            cancelled = self.cancellation.cancelled
            return self._failure(
                state,
                StopReason.CANCELLED if cancelled else StopReason.LIMIT_REACHED,
                "cancelled" if cancelled else "model_call_limit",
                "Citation repair cannot start"
                if repair
                else ("Turn was cancelled" if cancelled else "Maximum model calls reached"),
            )
        instruction = (
            repair_message(self.knowledge) if repair and self.knowledge is not None else None
        )
        try:
            if self.d.context is not None:
                request = await self.d.context.prepare_request(
                    self.knowledge, allow_tools=not repair, repair_message=instruction
                )
            else:
                request = self.d.builder.build(
                    self.d.journal.events,
                    self.d.model.capabilities,
                    self.knowledge,
                    allow_tools=not repair,
                    repair_message=instruction,
                )
        except (ContextLimitError, CompactionError) as error:
            return self._failure(state, StopReason.CONTEXT_LIMIT, "context_length", str(error))
        omitted = request.metadata.get("omitted_turn_ids")
        if omitted and not repair:
            await self.d.journal.record(
                state["turn_id"],
                RuntimeEventType.CONTEXT_WINDOW_SELECTED,
                ContextWindowSelectedPayload(tuple(omitted.split(","))),
            )
        state["request"] = request
        state["call_kind"] = "citation_repair" if repair else "task"
        state["response_text"], state["pending_calls"], state["tool_index"] = "", (), 0
        state["next_node"] = "repair_model" if repair else "model"
        return state

    async def prepare(self, state: _TurnState) -> _TurnState:
        return await self._prepare(state, repair=False)

    async def repair_prepare(self, state: _TurnState) -> _TurnState:
        return await self._prepare(state, repair=True)

    async def _consume(self, state: _TurnState, *, repair: bool) -> _TurnState:
        request = state["request"]
        assert request is not None
        state["model_calls"] += 1
        text_parts: list[str] = []
        calls: list[ToolCall] = []
        completed = False
        unfinished_delta = False
        finish: str | None = None
        try:
            async with asyncio.timeout(request.settings.timeout_seconds):
                async with closing_model_stream(self.d.model.stream(request)) as stream:
                    async for event in stream:
                        if repair:
                            if self.cancellation.cancelled:
                                raise asyncio.CancelledError
                        else:
                            for observer in self.d.observers:
                                await observer.observe(event)
                        if isinstance(event, TextDelta):
                            text_parts.append(event.text)
                        elif isinstance(event, (ToolCallDelta, ToolCallCompleted)):
                            if repair:
                                return self._invalid(state, "Citation repair cannot request tools")
                            if isinstance(event, ToolCallDelta):
                                unfinished_delta = True
                            else:
                                unfinished_delta = False
                                calls.append(event.call)
                        elif isinstance(event, UsageReported):
                            if self.d.context is not None:
                                await self.d.context.observe_usage(request, event.usage)
                            await self.d.journal.record(
                                state["turn_id"],
                                RuntimeEventType.MODEL_USAGE_RECORDED,
                                ModelUsageRecordedPayload(
                                    self.d.provider_name,
                                    request.model,
                                    event.usage,
                                    request.metadata.get("request_id"),
                                    "citation_repair"
                                    if repair
                                    else request.metadata.get("call_kind", "task"),
                                ),
                            )
                        elif isinstance(event, ResponseCompleted):
                            if completed and not repair:
                                return self._invalid(
                                    state, "Model emitted more than one completion event"
                                )
                            completed = event.finish_reason == "stop" if repair else True
                            finish = event.finish_reason
                        else:
                            if (
                                not repair
                                and event.category is ModelErrorCategory.CONTEXT_LENGTH
                                and self.d.context is not None
                                and not state["context_retried"]
                                and not calls
                                and not text_parts
                            ):
                                self.d.context.reject_window(request)
                                state["context_retried"] = True
                                state["next_node"] = "prepare"
                                return state
                            return self._failure(
                                state, StopReason.MODEL_ERROR, event.category.value, event.message
                            )
        except SessionError:
            raise
        except TimeoutError:
            return self._failure(state, StopReason.MODEL_ERROR, "timeout", "Model call timed out")
        except Exception as error:
            prefix = "Citation repair failed" if repair else "Model adapter failed"
            return self._failure(
                state,
                StopReason.MODEL_ERROR,
                "provider_internal",
                f"{prefix}: {type(error).__name__}",
            )
        if not completed or unfinished_delta:
            return self._invalid(state, "Model stream ended without a valid completion")
        if not repair:
            if finish == "length":
                return self._failure(
                    state,
                    StopReason.LIMIT_REACHED,
                    "model_finish_length",
                    "Model response reached its output limit",
                )
            if finish == "tool_calls" and not calls:
                return self._invalid(
                    state, "Model reported tool_calls without a complete tool call"
                )
            if calls and finish != "tool_calls":
                return self._invalid(
                    state, "Model emitted tool calls with an incompatible finish reason"
                )
            if not calls and finish != "stop":
                return self._failure(
                    state,
                    StopReason.MODEL_ERROR,
                    f"model_finish_{finish or 'unknown'}",
                    "Model response did not finish normally",
                )
        state["response_text"] = "".join(text_parts)
        state["pending_calls"] = tuple(calls)
        state["next_node"] = "citations"
        return state

    async def model(self, state: _TurnState) -> _TurnState:
        return await self._consume(state, repair=False)

    async def repair_model(self, state: _TurnState) -> _TurnState:
        return await self._consume(state, repair=True)

    async def citations(self, state: _TurnState) -> _TurnState:
        if not state["pending_calls"] and self.knowledge is not None:
            finalized = finalize_answer(state["response_text"], self.knowledge)
            if finalized.illegal:
                if state["repair_attempted"]:
                    return self._failure(
                        state,
                        StopReason.MODEL_ERROR,
                        "invalid_citation",
                        "Assistant citations were invalid after repair",
                    )
                state["repair_attempted"] = True
                state["next_node"] = "repair_prepare"
                return state
            state["response_text"] = finalized.display_text
            state["citations"] = finalized.citations
            state["model_supplement"] = finalized.model_supplement
        state["next_node"] = "persist"
        return state

    async def persist(self, state: _TurnState) -> _TurnState:
        text, calls = state["response_text"], state["pending_calls"]
        # Preserve the native result's tool prelude even if its IDs are rejected.
        state["turn_text_parts"].append(text)
        ids = [call.call_id for call in calls]
        if len(ids) != len(set(ids)) or any(call_id in state["seen_call_ids"] for call_id in ids):
            return self._invalid(state, "Model reused a tool call ID")
        await self.d.journal.record(
            state["turn_id"],
            RuntimeEventType.ASSISTANT_MESSAGE_COMPLETED,
            AssistantMessageCompletedPayload(
                text,
                calls,
                state["citations"] if not calls else (),
                state["model_supplement"] if not calls else "",
            ),
        )
        state["next_node"] = "tool" if calls else "complete"
        return state

    async def tool(self, state: _TurnState) -> _TurnState:
        if self.cancellation.cancelled:
            return self._failure(state, StopReason.CANCELLED, "cancelled", "Turn was cancelled")
        if state["tool_calls"] >= self.d.limits.max_tool_calls:
            return self._failure(
                state, StopReason.LIMIT_REACHED, "tool_call_limit", "Maximum tool calls reached"
            )
        index, calls = state["tool_index"], state["pending_calls"]
        call = calls[index]
        state["seen_call_ids"].add(call.call_id)
        await self.d.journal.record(
            state["turn_id"], RuntimeEventType.TOOL_CALL_REQUESTED, ToolCallRequestedPayload(call)
        )
        result = await self.d.tools.execute(call, self.tool_context)
        state["tool_calls"] += 1
        await self.d.journal.record(
            state["turn_id"],
            RuntimeEventType.TOOL_EXECUTION_COMPLETED,
            ToolExecutionCompletedPayload(result),
        )
        if result.error_code is ToolErrorCode.INSTRUCTIONS_DISCOVERED:
            state["tool_calls"] += len(calls) - index - 1
            await close_pending_calls(
                self.d.journal,
                state["turn_id"],
                reason="New directory rules require replanning the batch",
            )
            state["next_node"] = "prepare"
            return state
        if result.stop_turn:
            cancelled = result.outcome is not None and result.outcome.cancelled
            return self._failure(
                state,
                StopReason.CANCELLED if cancelled else StopReason.LIMIT_REACHED,
                "cancelled" if cancelled else "uncertain_side_effect",
                "Tool execution stopped the turn",
            )
        if result.outcome is not None and result.outcome.observed_failure:
            state["observed_failures"] += 1
            if state["observed_failures"] > self.d.limits.max_failure_recoveries:
                return self._failure(
                    state,
                    StopReason.LIMIT_REACHED,
                    "failure_recovery_limit",
                    "Failure recovery budget exhausted",
                )
        state["tool_index"] += 1
        state["next_node"] = "tool" if state["tool_index"] < len(calls) else "prepare"
        return state

    def _result(self, reason: StopReason, category: str | None = None) -> TurnResult:
        state = self.state
        return TurnResult(
            reason,
            "".join(state["turn_text_parts"]),
            state["model_calls"],
            state["tool_calls"],
            category,
            state["citations"],
            state["model_supplement"],
        )

    def _terminal_result(self) -> TurnResult | None:
        for event in reversed(self.d.journal.events):
            if event.turn_id != self.state["turn_id"]:
                continue
            payload = event.payload
            if isinstance(payload, (TurnCompletedPayload, TurnFailedPayload)):
                return TurnResult(
                    payload.stop_reason,
                    "".join(self.state["turn_text_parts"]),
                    payload.model_calls,
                    payload.tool_calls,
                    payload.error_category if isinstance(payload, TurnFailedPayload) else None,
                    self.state["citations"],
                    self.state["model_supplement"],
                )
        return None

    async def complete(self, state: _TurnState) -> _TurnState:
        await self.d.journal.record(
            state["turn_id"],
            RuntimeEventType.TURN_COMPLETED,
            TurnCompletedPayload(
                StopReason.COMPLETED,
                state["model_calls"],
                state["tool_calls"],
                self.d.provider_name,
                self.d.model_name,
            ),
        )
        state["result"] = self._result(StopReason.COMPLETED)
        return state

    async def fail(self, state: _TurnState) -> _TurnState:
        terminal = self._terminal_result()
        if terminal is not None:
            state["result"] = terminal
            return state
        self.finishing = True
        failure = state["failure"]
        assert failure is not None
        await close_pending_calls(self.d.journal, state["turn_id"])
        await self.d.journal.record(
            state["turn_id"],
            RuntimeEventType.TURN_FAILED,
            TurnFailedPayload(
                failure.reason,
                failure.category,
                failure.message,
                state["model_calls"],
                state["tool_calls"],
                self.d.provider_name,
                self.d.model_name,
            ),
        )
        state["result"] = self._result(failure.reason, failure.category)
        return state
