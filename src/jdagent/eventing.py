"""按序持久化会话事件，再将已保存的记录交给观察者。"""

from collections.abc import AsyncIterator

from jdagent.cancellation import complete_before_cancel
from jdagent.domain.errors import SessionError, SessionErrorCode
from jdagent.domain.events import (
    AssistantMessageCompletedPayload,
    MCPInvocationFinishedPayload,
    MCPInvocationStartedPayload,
    MCPTaskObservedPayload,
    MCPTaskRegisteredPayload,
    MCPTaskResultSavedPayload,
    RuntimeEvent,
    RuntimeEventType,
    RuntimePayload,
    ToolExecutionCompletedPayload,
    ToolExecutionStartedPayload,
)
from jdagent.domain.tools import RiskLevel, ToolErrorCode, ToolOutcome, ToolResult, ToolResultStatus
from jdagent.ports import EventObserver, RuntimeJournal, SessionPort


async def _collect(items: AsyncIterator[RuntimeEvent]) -> list[RuntimeEvent]:
    return [item async for item in items]


class EventJournal:
    """管理一个会话的事件序号。事件保存成功后才更新内存历史并通知观察者。"""

    def __init__(
        self,
        session: SessionPort,
        session_id: str,
        existing: list[RuntimeEvent],
        observers: tuple[EventObserver, ...],
    ) -> None:
        self._session = session
        self._session_id = session_id
        self._events = existing
        self._observers = observers

    @classmethod
    async def open(
        cls,
        session: SessionPort,
        session_id: str,
        *,
        observers: tuple[EventObserver, ...] = (),
        require_existing: bool = False,
    ) -> "EventJournal":
        """读取已有历史并建立日志视图；要求恢复已有会话时，会话缺失会报错。"""

        try:
            existing = await _collect(session.read(session_id))
        except SessionError as error:
            if error.code is not SessionErrorCode.NOT_FOUND or require_existing:
                raise
            existing = []
        return cls(session, session_id, existing, observers)

    @property
    def events(self) -> tuple[RuntimeEvent, ...]:
        return tuple(self._events)

    async def record(
        self,
        turn_id: str | None,
        event_type: RuntimeEventType,
        payload: RuntimePayload,
    ) -> RuntimeEvent:
        """使用当前会话标识和下一个序号创建事件，并按日志的写入顺序发布。"""

        event = RuntimeEvent.create(
            session_id=self._session_id,
            turn_id=turn_id,
            sequence=len(self._events) + 1,
            event_type=event_type,
            payload=payload,
        )
        await self.emit(event)
        return event

    async def emit(self, event: RuntimeEvent) -> None:
        """校验待写事件的会话与序号，追加成功后再通知观察者。"""

        if event.session_id != self._session_id:
            raise SessionError(SessionErrorCode.APPEND_FAILED, "Event belongs to another session")
        expected = len(self._events) + 1
        if event.sequence != expected:
            raise SessionError(
                SessionErrorCode.APPEND_FAILED,
                f"Expected journal sequence {expected}, received {event.sequence}",
            )
        # 取消等待无法中止线程里的文件追加，必须等写入和内存序号更新一同完成。
        await complete_before_cancel(self._persist(event))
        for observer in self._observers:
            await observer.observe(event)

    async def _persist(self, event: RuntimeEvent) -> None:
        await self._session.append(event)
        self._events.append(event)


async def close_pending_calls(
    journal: RuntimeJournal,
    turn_id: str,
    *,
    reason: str = "Turn ended before a result was available",
) -> None:
    """在一批工具调用中补齐尚无结果的记录，使恢复时能区分未执行的调用与结果未知的调用。"""
    events = [event for event in journal.events if event.turn_id == turn_id]
    completed = {
        event.payload.result.call_id
        for event in events
        if isinstance(event.payload, ToolExecutionCompletedPayload)
    }
    started = {
        event.payload.call_id
        for event in events
        if isinstance(event.payload, ToolExecutionStartedPayload)
        and event.payload.risk in {RiskLevel.WRITE, RiskLevel.EXECUTE}
    }
    mcp_intents = {
        event.payload.call_id: event.payload
        for event in events
        if isinstance(event.payload, MCPInvocationStartedPayload)
    }
    mcp_finished = {
        event.payload.result.call_id: event.payload.result
        for event in events
        if isinstance(event.payload, MCPInvocationFinishedPayload)
    }
    task_references = {
        event.payload.call_id: event.payload
        for event in events
        if isinstance(event.payload, MCPTaskRegisteredPayload)
    }
    requested_calls = {
        call.call_id: call
        for event in events
        if isinstance(event.payload, AssistantMessageCompletedPayload)
        for call in event.payload.tool_calls
    }
    for event in journal.events:
        payload = event.payload
        if not isinstance(payload, MCPTaskResultSavedPayload) or payload.origin_turn_id != turn_id:
            continue
        registration = task_references.get(payload.call_id)
        call = requested_calls.get(payload.call_id)
        if registration is None or registration.task_ref != payload.task_ref or call is None:
            continue
        outcome = payload.outcome
        # 修复未配对的模型调用前，管理操作可能已保存结果，应保留已知的调用结局。
        mcp_finished.setdefault(
            payload.call_id,
            ToolResult(
                payload.call_id,
                call.name,
                ToolResultStatus.ERROR if outcome.error_code else ToolResultStatus.SUCCESS,
                error_code=outcome.error_code,
                error_message=outcome.message or None,
                outcome=outcome,
                metadata={"tool_origin": outcome.data.get("source")},
            ),
        )
    task_statuses = {
        event.payload.task_ref: event.payload.status
        for event in journal.events
        if isinstance(event.payload, MCPTaskObservedPayload) and event.payload.status is not None
    }
    for event in events:
        if not isinstance(event.payload, AssistantMessageCompletedPayload):
            continue
        for call in event.payload.tool_calls:
            if call.call_id in completed:
                continue
            unknown = call.call_id in started
            intent = mcp_intents.get(call.call_id)
            task = task_references.get(call.call_id)
            outcome = ToolOutcome(
                data={
                    "task_ref": task.task_ref,
                    "task_status": task_statuses.get(task.task_ref, task.status),
                }
                if task is not None
                else {},
                completed=False,
                effect="unknown" if unknown else "none",
                error_code=ToolErrorCode.UNCERTAIN
                if unknown or intent
                else ToolErrorCode.NOT_EXECUTED,
                message=reason,
                requires_stop=intent is not None,
            )
            result = mcp_finished.get(call.call_id) or ToolResult(
                call.call_id,
                call.name,
                ToolResultStatus.ERROR,
                error_code=outcome.error_code,
                error_message=outcome.message,
                outcome=outcome,
                metadata={
                    "tool_origin": {
                        "kind": "mcp",
                        "server_id": intent.server_id,
                        "raw_name": intent.raw_name,
                        "generation": intent.generation,
                    }
                }
                if intent
                else {},
            )
            await journal.record(
                turn_id,
                RuntimeEventType.TOOL_EXECUTION_COMPLETED,
                ToolExecutionCompletedPayload(result),
            )
            completed.add(call.call_id)
