"""从已保存的会话事件生成跟踪信息，控制其中可展示的内容。"""

from dataclasses import dataclass
from datetime import datetime

from jdagent.domain.errors import StopReason
from jdagent.domain.events import (
    ModelUsageRecordedPayload,
    PermissionResolvedPayload,
    RuntimeEvent,
    RuntimeEventType,
    ToolExecutionCompletedPayload,
    TurnCompletedPayload,
    TurnFailedPayload,
    TurnRetrievalRecordedPayload,
)


@dataclass(frozen=True, slots=True)
class TraceEntry:
    """保存一条持久事件的标识和计时信息，供追踪使用。"""

    event_id: str
    session_id: str
    turn_id: str | None
    sequence: int
    event_type: RuntimeEventType
    timestamp: datetime
    tool_status: str | None = None
    permission_policy: str | None = None
    approval_decision: str | None = None
    stop_reason: StopReason | None = None
    error_category: str | None = None


@dataclass(frozen=True, slots=True)
class TraceSummary:
    """汇总已观察事件的耗时、用量和调用情况，供界面或脚本展示。"""

    event_count: int
    provider: str | None
    model: str | None
    model_calls: int
    tool_calls: int
    input_tokens: int
    output_tokens: int
    stop_reason: StopReason | None
    error_category: str | None
    duration_ms: int
    knowledge_outcome: str | None = None


class TraceProjection:
    """从已经持久化的事件提取追踪信息，避免再次保存消息或工具的完整正文。"""

    def __init__(self) -> None:
        self._entries: list[TraceEntry] = []
        self._provider: str | None = None
        self._model: str | None = None
        self._model_calls = 0
        self._tool_calls = 0
        self._input_tokens = 0
        self._output_tokens = 0
        self._stop_reason: StopReason | None = None
        self._error_category: str | None = None
        self._knowledge_outcome: str | None = None

    @property
    def entries(self) -> tuple[TraceEntry, ...]:
        return tuple(self._entries)

    @property
    def summary(self) -> TraceSummary:
        """返回当前汇总指标，其中不包含原始事件载荷。"""

        duration_ms = 0
        if len(self._entries) >= 2:
            delta = self._entries[-1].timestamp - self._entries[0].timestamp
            duration_ms = max(0, round(delta.total_seconds() * 1000))
        return TraceSummary(
            len(self._entries),
            self._provider,
            self._model,
            self._model_calls,
            self._tool_calls,
            self._input_tokens,
            self._output_tokens,
            self._stop_reason,
            self._error_category,
            duration_ms,
            self._knowledge_outcome,
        )

    async def observe(self, event: RuntimeEvent) -> None:
        """按事件类型更新追踪条目和累计量，使追踪结果与写入顺序一致。"""
        payload = event.payload
        tool_status = (
            payload.result.status.value
            if isinstance(payload, ToolExecutionCompletedPayload)
            else None
        )
        permission_policy = (
            payload.policy.value if isinstance(payload, PermissionResolvedPayload) else None
        )
        approval_decision = (
            payload.approval.value
            if isinstance(payload, PermissionResolvedPayload) and payload.approval is not None
            else None
        )
        stop_reason = (
            payload.stop_reason
            if isinstance(payload, TurnCompletedPayload | TurnFailedPayload)
            else None
        )
        error_category = payload.error_category if isinstance(payload, TurnFailedPayload) else None
        self._entries.append(
            TraceEntry(
                event.event_id,
                event.session_id,
                event.turn_id,
                event.sequence,
                event.event_type,
                event.timestamp,
                tool_status,
                permission_policy,
                approval_decision,
                stop_reason,
                error_category,
            )
        )
        if isinstance(payload, ModelUsageRecordedPayload):
            self._provider = payload.provider
            self._model = payload.model
            self._input_tokens += payload.usage.input_tokens
            self._output_tokens += payload.usage.output_tokens
        elif isinstance(payload, TurnRetrievalRecordedPayload):
            self._knowledge_outcome = payload.outcome
        elif isinstance(payload, TurnCompletedPayload):
            self._provider = payload.provider
            self._model = payload.model
            self._model_calls = payload.model_calls
            self._tool_calls = payload.tool_calls
            self._stop_reason = payload.stop_reason
        elif isinstance(payload, TurnFailedPayload):
            self._provider = payload.provider
            self._model = payload.model
            self._model_calls = payload.model_calls
            self._tool_calls = payload.tool_calls
            self._stop_reason = payload.stop_reason
            self._error_category = payload.error_category
