"""通过顺序执行的 LangGraph 节点推进一轮对话，持久历史由会话事件日志保存。"""

import asyncio
from collections.abc import Awaitable
from dataclasses import dataclass, replace
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
    ModelFailed,
    ModelRequest,
    UsageReported,
)
from jdagent.domain.tools import ToolCall, ToolErrorCode, ToolExecutionContext
from jdagent.eventing import close_pending_calls
from jdagent.knowledge.citation import finalize_answer, repair_message
from jdagent.knowledge.ptk import PreparedTurnKnowledge
from jdagent.model_stream import closing_model_stream
from jdagent.ports import ModelPort, RuntimeJournal, ToolRuntimePort
from jdagent.runtime.model_response import ModelResponse, ResponseProtocolError


@dataclass(frozen=True, slots=True)
class _Failure:
    """保存失败节点交给收尾节点的停止原因、错误类别和说明。"""

    reason: StopReason
    category: str
    message: str


# 图在一轮内传递的状态。调用预算按整轮累计，待执行工具和响应文本属于当前模型请求。
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
    """保存图节点共用的模型、工具和上下文服务，以及当前会话的事件日志。"""

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
    # 每次模型尝试最多经过六个节点，每个已执行工具再占一步；另为上限检查、
    # 引用修正和结束节点留出步数。
    return 6 * limits.max_model_calls + 2 * limits.max_tool_calls + 10


def _route(state: _TurnState) -> str:
    return state["next_node"]


class LangGraphTurnRunner:
    """使用会话级服务执行一轮对话。每次调用都创建独立的图状态，计数器与待执行工具按轮隔离。"""

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
        """为本轮绑定工具上下文、取消信号与知识材料，返回图执行得到的终态。"""
        execution = _Execution(
            self._dependencies,
            turn_id,
            tool_context,
            cancellation or CancellationToken(),
            knowledge,
        )
        return await execution.run()


class _Execution:
    """持有一次图调用的可变状态，并提供构成这张图的节点函数。"""

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
            # 从会话历史还原已使用的调用标识，防止后续模型响应重复使用这些标识。
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
            # 节点按顺序写入，且不会从检查点重放。即使等待过程中抛出异常，也要保留此前的进度。
            self.stage, self.state = name, state
            return await node(state)

        return execute

    async def run(self) -> TurnResult:
        """建立节点及允许的下一步，执行直到成功或失败。发生异常时，先检查日志中是否已有终态。"""
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
            # 如果写日志本身失败，就没有证据表明这次失败事件已被保存。
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
        """在模型调用之前检查取消与预算，并按普通回答或引用修复准备本次请求。"""
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
        """读取一次模型流，分别处理显示、用量和响应内容；确认流完整后才交给引用校验或工具阶段。"""
        request = state["request"]
        assert request is not None
        state["model_calls"] += 1
        response = ModelResponse(repair=repair)
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
                        if isinstance(event, UsageReported):
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
                        elif isinstance(event, ModelFailed):
                            if (
                                not repair
                                and event.category is ModelErrorCategory.CONTEXT_LENGTH
                                and self.d.context is not None
                                and not state["context_retried"]
                                and not response.calls
                                and not response.text_parts
                            ):
                                self.d.context.reject_window(request)
                                state["context_retried"] = True
                                state["next_node"] = "prepare"
                                return state
                            return self._failure(
                                state, StopReason.MODEL_ERROR, event.category.value, event.message
                            )
                        else:
                            response.accept(event)
            response.validate()
        except ResponseProtocolError as error:
            return self._failure(state, error.reason, error.category, str(error))
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
        state["response_text"] = "".join(response.text_parts)
        state["pending_calls"] = tuple(response.calls)
        state["next_node"] = "citations"
        return state

    async def model(self, state: _TurnState) -> _TurnState:
        return await self._consume(state, repair=False)

    async def repair_model(self, state: _TurnState) -> _TurnState:
        return await self._consume(state, repair=True)

    async def citations(self, state: _TurnState) -> _TurnState:
        """核对最终文本中的引用。发现无效引用时允许模型修复一次，仍无效则结束本轮。"""
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
        """保存完整的助手消息。把调用交给工具前，先检查同批和已发起调用的标识是否重复。"""
        text, calls = state["response_text"], state["pending_calls"]
        # 即使工具调用标识无效，模型在提出调用前已经输出的文本仍属于本轮结果。
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
        """依次执行当前批次的工具并记录结果。根据失败次数、停止标记或新发现的目录规则决定下一步。"""
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
        request = state["request"]
        assert request is not None
        result = await self.d.tools.execute(
            call,
            replace(
                self.tool_context,
                request_id=request.metadata.get("request_id", ""),
                external_context=request.metadata.get("external_context") == "mcp",
            ),
        )
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
        """从日志中查找本轮已写入的终态，避免观察者出错后再次记录结束。"""
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
        """写入成功终态，并返回整轮累计的文本与调用次数。"""
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
        """先为尚未配对的工具调用补记结果，再保存失败原因并生成本轮结果。"""
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
