"""The small state machine that drives one agent turn."""

import asyncio
from dataclasses import dataclass
from typing import Protocol

from jdagent.context import ContextBuilder, ContextLimitError
from jdagent.domain.errors import StopReason
from jdagent.domain.events import (
    AssistantMessageCompletedPayload,
    ModelUsageRecordedPayload,
    RuntimeEventType,
    ToolCallRequestedPayload,
    ToolExecutionCompletedPayload,
    TurnCompletedPayload,
    TurnFailedPayload,
)
from jdagent.domain.model import (
    ModelErrorCategory,
    ModelEvent,
    ResponseCompleted,
    TextDelta,
    ToolCallCompleted,
    ToolCallDelta,
    UsageReported,
)
from jdagent.domain.tools import ToolCall, ToolExecutionContext
from jdagent.ports import ModelPort, RuntimeJournal, ToolRuntimePort


class ModelEventObserver(Protocol):
    """Consume transient model events such as displayable text deltas."""

    async def observe(self, event: ModelEvent) -> None: ...


@dataclass(frozen=True, slots=True)
class LoopLimits:
    """Hard call-count limits for one turn."""

    max_model_calls: int = 8
    max_tool_calls: int = 16

    def __post_init__(self) -> None:
        if self.max_model_calls <= 0 or self.max_tool_calls <= 0:
            raise ValueError("Loop limits must be positive")


@dataclass(frozen=True, slots=True)
class TurnResult:
    """The caller-visible result of one complete turn."""

    stop_reason: StopReason
    assistant_text: str
    model_calls: int
    tool_calls: int
    error_category: str | None = None


class CancellationToken:
    """A cooperative cancellation signal checked at safe loop boundaries."""

    def __init__(self) -> None:
        self._cancelled = False

    @property
    def cancelled(self) -> bool:
        return self._cancelled

    def cancel(self) -> None:
        self._cancelled = True


class AgentLoop:
    """Drive model and tool calls without owning adapters or persistence formats."""

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
    ) -> None:
        self._model = model
        self._context_builder = context_builder
        self._tools = tools
        self._journal = journal
        self._limits = limits or LoopLimits()
        self._provider_name = provider_name
        self._model_name = model_name
        self._model_event_observers = model_event_observers

    async def run(
        self,
        turn_id: str,
        tool_context: ToolExecutionContext,
        *,
        cancellation: CancellationToken | None = None,
    ) -> TurnResult:
        """Run until normal completion, failure, cancellation, or a hard limit."""

        cancellation = cancellation or CancellationToken()
        model_calls = 0
        tool_calls = 0
        text_parts: list[str] = []
        seen_call_ids: set[str] = set()

        while True:
            if cancellation.cancelled:
                return await self._fail(
                    turn_id,
                    StopReason.CANCELLED,
                    ModelErrorCategory.CANCELLED.value,
                    "Turn was cancelled",
                    model_calls,
                    tool_calls,
                    text_parts,
                )
            if model_calls >= self._limits.max_model_calls:
                return await self._fail(
                    turn_id,
                    StopReason.LIMIT_REACHED,
                    "model_call_limit",
                    "Maximum model calls reached",
                    model_calls,
                    tool_calls,
                    text_parts,
                )

            try:
                request = self._context_builder.build(
                    self._journal.events,
                    self._model.capabilities,
                )
            except ContextLimitError as error:
                return await self._fail(
                    turn_id,
                    StopReason.CONTEXT_LIMIT,
                    "context_length",
                    str(error),
                    model_calls,
                    tool_calls,
                    text_parts,
                )

            model_calls += 1
            response_text: list[str] = []
            response_calls: list[ToolCall] = []
            completed = False
            finish_reason: str | None = None
            unfinished_tool_delta = False
            try:
                async with asyncio.timeout(request.settings.timeout_seconds):
                    async for event in self._model.stream(request):
                        for observer in self._model_event_observers:
                            await observer.observe(event)
                        if isinstance(event, TextDelta):
                            response_text.append(event.text)
                        elif isinstance(event, ToolCallDelta):
                            unfinished_tool_delta = True
                        elif isinstance(event, ToolCallCompleted):
                            unfinished_tool_delta = False
                            response_calls.append(event.call)
                        elif isinstance(event, UsageReported):
                            await self._journal.record(
                                turn_id,
                                RuntimeEventType.MODEL_USAGE_RECORDED,
                                ModelUsageRecordedPayload(
                                    self._provider_name,
                                    request.model,
                                    event.usage,
                                ),
                            )
                        elif isinstance(event, ResponseCompleted):
                            if completed:
                                return await self._model_protocol_failure(
                                    turn_id,
                                    "Model emitted more than one completion event",
                                    model_calls,
                                    tool_calls,
                                    text_parts,
                                )
                            completed = True
                            finish_reason = event.finish_reason
                        else:
                            return await self._fail(
                                turn_id,
                                StopReason.MODEL_ERROR,
                                event.category.value,
                                event.message,
                                model_calls,
                                tool_calls,
                                text_parts,
                            )
            except TimeoutError:
                return await self._fail(
                    turn_id,
                    StopReason.MODEL_ERROR,
                    ModelErrorCategory.TIMEOUT.value,
                    "Model call timed out",
                    model_calls,
                    tool_calls,
                    text_parts,
                )
            except asyncio.CancelledError:
                await self._record_failure(
                    turn_id,
                    StopReason.CANCELLED,
                    ModelErrorCategory.CANCELLED.value,
                    "Turn task was cancelled",
                    model_calls,
                    tool_calls,
                    text_parts,
                )
                raise
            except Exception as error:
                return await self._fail(
                    turn_id,
                    StopReason.MODEL_ERROR,
                    ModelErrorCategory.PROVIDER_INTERNAL.value,
                    f"Model adapter failed: {type(error).__name__}",
                    model_calls,
                    tool_calls,
                    text_parts,
                )

            if not completed or unfinished_tool_delta:
                return await self._model_protocol_failure(
                    turn_id,
                    "Model stream ended without a valid completion",
                    model_calls,
                    tool_calls,
                    text_parts,
                )

            if finish_reason == "length":
                return await self._fail(
                    turn_id,
                    StopReason.LIMIT_REACHED,
                    "model_finish_length",
                    "Model response reached its output limit",
                    model_calls,
                    tool_calls,
                    text_parts,
                )
            if finish_reason == "tool_calls" and not response_calls:
                return await self._model_protocol_failure(
                    turn_id,
                    "Model reported tool_calls without a complete tool call",
                    model_calls,
                    tool_calls,
                    text_parts,
                )
            if response_calls and finish_reason != "tool_calls":
                return await self._model_protocol_failure(
                    turn_id,
                    "Model emitted tool calls with an incompatible finish reason",
                    model_calls,
                    tool_calls,
                    text_parts,
                )
            if not response_calls and finish_reason != "stop":
                return await self._fail(
                    turn_id,
                    StopReason.MODEL_ERROR,
                    f"model_finish_{finish_reason or 'unknown'}",
                    "Model response did not finish normally",
                    model_calls,
                    tool_calls,
                    text_parts,
                )

            text = "".join(response_text)
            text_parts.append(text)
            response_call_ids = [call.call_id for call in response_calls]
            if len(response_call_ids) != len(set(response_call_ids)) or any(
                call_id in seen_call_ids for call_id in response_call_ids
            ):
                return await self._model_protocol_failure(
                    turn_id,
                    "Model reused a tool call ID",
                    model_calls,
                    tool_calls,
                    text_parts,
                )
            await self._journal.record(
                turn_id,
                RuntimeEventType.ASSISTANT_MESSAGE_COMPLETED,
                AssistantMessageCompletedPayload(text, tuple(response_calls)),
            )
            if not response_calls:
                await self._journal.record(
                    turn_id,
                    RuntimeEventType.TURN_COMPLETED,
                    TurnCompletedPayload(
                        StopReason.COMPLETED,
                        model_calls,
                        tool_calls,
                        self._provider_name,
                        self._model_name,
                    ),
                )
                return TurnResult(
                    StopReason.COMPLETED,
                    "".join(text_parts),
                    model_calls,
                    tool_calls,
                )

            for call in response_calls:
                if cancellation.cancelled:
                    return await self._fail(
                        turn_id,
                        StopReason.CANCELLED,
                        ModelErrorCategory.CANCELLED.value,
                        "Turn was cancelled",
                        model_calls,
                        tool_calls,
                        text_parts,
                    )
                if tool_calls >= self._limits.max_tool_calls:
                    return await self._fail(
                        turn_id,
                        StopReason.LIMIT_REACHED,
                        "tool_call_limit",
                        "Maximum tool calls reached",
                        model_calls,
                        tool_calls,
                        text_parts,
                    )
                seen_call_ids.add(call.call_id)
                await self._journal.record(
                    turn_id,
                    RuntimeEventType.TOOL_CALL_REQUESTED,
                    ToolCallRequestedPayload(call),
                )
                try:
                    result = await self._tools.execute(call, tool_context)
                except asyncio.CancelledError:
                    await self._record_failure(
                        turn_id,
                        StopReason.CANCELLED,
                        ModelErrorCategory.CANCELLED.value,
                        "Turn task was cancelled during tool execution",
                        model_calls,
                        tool_calls,
                        text_parts,
                    )
                    raise
                tool_calls += 1
                await self._journal.record(
                    turn_id,
                    RuntimeEventType.TOOL_EXECUTION_COMPLETED,
                    ToolExecutionCompletedPayload(result),
                )

    async def _model_protocol_failure(
        self,
        turn_id: str,
        message: str,
        model_calls: int,
        tool_calls: int,
        text_parts: list[str],
    ) -> TurnResult:
        return await self._fail(
            turn_id,
            StopReason.MODEL_ERROR,
            ModelErrorCategory.INVALID_RESPONSE.value,
            message,
            model_calls,
            tool_calls,
            text_parts,
        )

    async def _fail(
        self,
        turn_id: str,
        stop_reason: StopReason,
        error_category: str,
        message: str,
        model_calls: int,
        tool_calls: int,
        text_parts: list[str],
    ) -> TurnResult:
        await self._record_failure(
            turn_id,
            stop_reason,
            error_category,
            message,
            model_calls,
            tool_calls,
            text_parts,
        )
        return TurnResult(
            stop_reason,
            "".join(text_parts),
            model_calls,
            tool_calls,
            error_category,
        )

    async def _record_failure(
        self,
        turn_id: str,
        stop_reason: StopReason,
        error_category: str,
        message: str,
        model_calls: int,
        tool_calls: int,
        text_parts: list[str],
    ) -> None:
        del text_parts
        await self._journal.record(
            turn_id,
            RuntimeEventType.TURN_FAILED,
            TurnFailedPayload(
                stop_reason,
                error_category,
                message,
                model_calls,
                tool_calls,
                self._provider_name,
                self._model_name,
            ),
        )
