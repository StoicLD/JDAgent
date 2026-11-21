"""分别判断日志文件是否完整，以及上次中断的对话能否安全继续。"""

import json
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Protocol
from uuid import uuid4

from jdagent.cancellation import complete_before_cancel
from jdagent.context_archive import prefix_hash
from jdagent.domain.errors import SessionError, SessionErrorCode, StopReason
from jdagent.domain.events import (
    AssistantMessageCompletedPayload,
    CompactionCommittedPayload,
    CompactionFailedPayload,
    ModelRequestPreparedPayload,
    ModelUsageRecordedPayload,
    RecoverySnapshotPayload,
    RuntimeEvent,
    RuntimeEventType,
    SessionStartedPayload,
    ToolExecutionCompletedPayload,
    ToolExecutionStartedPayload,
    TurnFailedPayload,
)
from jdagent.domain.tools import RiskLevel, command_log_id
from jdagent.eventing import EventJournal, close_pending_calls
from jdagent.ports import SessionPort


class PhysicalRecovery(StrEnum):
    """表示存储末尾是否需要修复，以及能否在保留有效记录的前提下修复。"""

    NO_REPAIR_NEEDED = "no_repair_needed"
    REPAIRED_FINAL_PARTIAL_RECORD = "repaired_final_partial_record"
    UNRECOVERABLE = "unrecoverable"


class LogicalRecovery(StrEnum):
    """表示已有完整事件能否支持安全继续，尤其关注中断时可能已经发生的副作用。"""

    CLEAN = "clean"
    INTERRUPTED_SAFE = "interrupted_safe"
    UNCERTAIN_SIDE_EFFECT = "uncertain_side_effect"


@dataclass(frozen=True, slots=True)
class PhysicalRecoveryResult:
    """保存物理检查的结论，以及成功读取或修复后可用的事件序列。"""

    physical: PhysicalRecovery
    events: tuple[RuntimeEvent, ...]
    backup_path: Path | None = None
    byte_offset: int | None = None
    message: str | None = None


class RecoveryStorePort(Protocol):
    """检查历史记录的物理完整性，可修复文件末尾未写完的记录。"""

    async def recover_physical(self, session_id: str) -> PhysicalRecoveryResult: ...

    def ownership(self, session_id: str) -> AbstractAsyncContextManager[None]: ...


@dataclass(frozen=True, slots=True)
class RecoveryResult:
    """汇总存储检查与业务恢复判断，供上层决定继续原会话还是隔离未知操作。"""

    physical: PhysicalRecovery
    logical: LogicalRecovery | None
    events: tuple[RuntimeEvent, ...]
    backup_path: Path | None = None
    byte_offset: int | None = None
    message: str | None = None


@dataclass(frozen=True, slots=True)
class PreparedResume:
    """给调用方返回可继续使用的会话标识，以及需要呈现给用户的恢复提示。"""

    session_id: str
    warnings: tuple[str, ...] = ()


def _turn_tail(events: tuple[RuntimeEvent, ...]) -> tuple[RuntimeEvent, ...]:
    terminal_types = {RuntimeEventType.TURN_COMPLETED, RuntimeEventType.TURN_FAILED}
    last_terminal = max(
        (index for index, event in enumerate(events) if event.event_type in terminal_types),
        default=-1,
    )
    # 空闲压缩的记账事件可能沿用上一轮标识，判断前台对话是否中断时应跳过这些事件。
    return tuple(
        event
        for event in events[last_terminal + 1 :]
        if not isinstance(event.payload, (CompactionCommittedPayload, CompactionFailedPayload))
        and not (
            isinstance(event.payload, (ModelRequestPreparedPayload, ModelUsageRecordedPayload))
            and event.payload.call_kind == "compaction"
        )
    )


def classify_logical_recovery(events: tuple[RuntimeEvent, ...]) -> LogicalRecovery:
    """检查最后一轮中已请求、已开始和已完成的操作，判断继续执行是否可能重复副作用。"""

    started_all = {
        event.payload.call_id: event.payload.risk
        for event in events
        if isinstance(event.payload, ToolExecutionStartedPayload)
    }
    completed_all = {
        event.payload.result.call_id: event.payload.result
        for event in events
        if isinstance(event.payload, ToolExecutionCompletedPayload)
    }
    if any(
        result.outcome is not None and result.outcome.effect == "unknown"
        for result in completed_all.values()
    ):
        return LogicalRecovery.UNCERTAIN_SIDE_EFFECT
    if any(
        risk not in {RiskLevel.PURE, RiskLevel.READ}
        for call_id, risk in started_all.items()
        if call_id not in completed_all
    ):
        return LogicalRecovery.UNCERTAIN_SIDE_EFFECT
    tail = _turn_tail(events)
    if not any(event.turn_id is not None for event in tail):
        return LogicalRecovery.CLEAN
    started = {
        event.payload.call_id: event.payload.risk
        for event in tail
        if isinstance(event.payload, ToolExecutionStartedPayload)
    }
    completed = {
        event.payload.result.call_id
        for event in tail
        if isinstance(event.payload, ToolExecutionCompletedPayload)
    }
    unresolved_risks = [risk for call_id, risk in started.items() if call_id not in completed]
    if any(risk not in {RiskLevel.PURE, RiskLevel.READ} for risk in unresolved_risks):
        return LogicalRecovery.UNCERTAIN_SIDE_EFFECT
    return LogicalRecovery.INTERRUPTED_SAFE


class SessionRecovery:
    """先检查会话文件的物理完整性，再根据有效事件判断业务状态。"""

    def __init__(self, store: RecoveryStorePort) -> None:
        self._store = store

    def ownership(self, session_id: str) -> AbstractAsyncContextManager[None]:
        """在检查和写入恢复结果期间持有会话，防止作为判断依据的日志发生变化。"""
        return self._store.ownership(session_id)

    async def recover(self, session_id: str) -> RecoveryResult:
        physical = await self._store.recover_physical(session_id)
        logical = (
            None
            if physical.physical is PhysicalRecovery.UNRECOVERABLE
            else classify_logical_recovery(physical.events)
        )
        return RecoveryResult(
            physical=physical.physical,
            logical=logical,
            events=physical.events,
            backup_path=physical.backup_path,
            byte_offset=physical.byte_offset,
            message=physical.message,
        )


async def create_recovery_session(
    *,
    session: SessionPort,
    source_events: tuple[RuntimeEvent, ...],
    workspace_identity: str,
    new_session_id: str | None = None,
) -> str:
    """以最后一个安全终态为界创建独立的恢复会话，并在快照中列出仍需核实的操作。"""

    if not source_events:
        raise ValueError("Recovery source must contain events")
    terminal_types = {RuntimeEventType.TURN_COMPLETED, RuntimeEventType.TURN_FAILED}
    terminal_indexes = [
        index for index, event in enumerate(source_events) if event.event_type in terminal_types
    ]
    safe_index = next(
        (
            index
            for index in reversed(terminal_indexes)
            if classify_logical_recovery(source_events[: index + 1])
            is not LogicalRecovery.UNCERTAIN_SIDE_EFFECT
        ),
        0,
    )
    safe_events = source_events[: safe_index + 1]
    completed = {
        event.payload.result.call_id: event.payload.result
        for event in source_events
        if isinstance(event.payload, ToolExecutionCompletedPayload)
    }
    unknown: list[str] = []
    for event in source_events:
        if not isinstance(event.payload, ToolExecutionStartedPayload):
            continue
        payload = event.payload
        result = completed.get(payload.call_id)
        if (result is None and payload.risk not in {RiskLevel.PURE, RiskLevel.READ}) or (
            result is not None and result.outcome is not None and result.outcome.effect == "unknown"
        ):
            unknown.append(
                json.dumps(
                    {
                        "call_id": payload.call_id,
                        "tool": payload.tool_name,
                        "effect": "unknown",
                        "log_id": command_log_id(payload.call_id)
                        if payload.risk is RiskLevel.EXECUTE
                        else None,
                    },
                    ensure_ascii=False,
                )
            )
    checkpoint = next(
        (
            event.payload.object_hash
            for event in reversed(source_events)
            if isinstance(event.payload, CompactionCommittedPayload)
            and event.payload.through_sequence <= safe_events[-1].sequence
            and event.payload.prefix_hash
            == prefix_hash(source_events, event.payload.through_sequence)
        ),
        None,
    )
    actual_session_id = new_session_id or str(uuid4())
    journal = await EventJournal.open(session, actual_session_id)
    await journal.record(
        None,
        RuntimeEventType.SESSION_STARTED,
        SessionStartedPayload(
            name=f"recovery-{source_events[0].session_id[:8]}",
            workspace_identity=workspace_identity,
        ),
    )
    await journal.record(
        None,
        RuntimeEventType.RECOVERY_SNAPSHOT,
        RecoverySnapshotPayload(
            parent_session_id=source_events[0].session_id,
            through_sequence=safe_events[-1].sequence,
            prefix_hash=prefix_hash(source_events, safe_events[-1].sequence),
            checkpoint_hash=checkpoint,
            unknown_operations=tuple(unknown),
            gap_until=source_events[-1].sequence,
        ),
    )
    return actual_session_id


async def close_interrupted_turn(
    *,
    session: SessionPort,
    events: tuple[RuntimeEvent, ...],
) -> RuntimeEvent:
    """为可以安全收尾的中断轮次追加一次明确的终态。"""

    if classify_logical_recovery(events) is not LogicalRecovery.INTERRUPTED_SAFE:
        raise ValueError("Only a safely interrupted turn can be closed automatically")
    tail = _turn_tail(events)
    turn_ids = [event.turn_id for event in tail if event.turn_id is not None]
    if not turn_ids:
        raise ValueError("Interrupted session has no active turn")
    active_turn_id = turn_ids[-1]
    if any(turn_id != active_turn_id for turn_id in turn_ids):
        raise ValueError("Interrupted session contains multiple open turns")
    model_calls = sum(isinstance(event.payload, AssistantMessageCompletedPayload) for event in tail)
    tool_calls = sum(isinstance(event.payload, ToolExecutionCompletedPayload) for event in tail)
    journal = await EventJournal.open(
        session,
        events[0].session_id,
        require_existing=True,
    )
    await close_pending_calls(journal, active_turn_id)
    return await journal.record(
        active_turn_id,
        RuntimeEventType.TURN_FAILED,
        TurnFailedPayload(
            stop_reason=StopReason.CANCELLED,
            error_category="process_interrupted",
            message="Previous process ended before the turn completed",
            model_calls=model_calls,
            tool_calls=tool_calls,
        ),
    )


async def prepare_session_resume(
    *,
    session: SessionPort,
    recovery: SessionRecovery,
    session_id: str,
    workspace_identity: str,
    allow_recovery_snapshot: bool,
) -> PreparedResume:
    """检查存储尾部与最后一轮，关闭可安全收尾的中断；副作用不明时按调用方许可创建恢复会话。"""

    async with recovery.ownership(session_id):
        return await complete_before_cancel(
            _prepare_owned_resume(
                session=session,
                recovery=recovery,
                session_id=session_id,
                workspace_identity=workspace_identity,
                allow_recovery_snapshot=allow_recovery_snapshot,
            )
        )


async def _prepare_owned_resume(
    *,
    session: SessionPort,
    recovery: SessionRecovery,
    session_id: str,
    workspace_identity: str,
    allow_recovery_snapshot: bool,
) -> PreparedResume:
    """在已取得会话所有权后检查上次中断；可安全收尾时沿用原会话，需要隔离未知操作时创建恢复会话。"""
    result = await recovery.recover(session_id)
    if result.physical is PhysicalRecovery.UNRECOVERABLE:
        raise SessionError(
            SessionErrorCode.CORRUPT_EVENT,
            result.message or f"Session cannot be recovered: {session_id}",
        )
    warnings: list[str] = []
    if result.physical is PhysicalRecovery.REPAIRED_FINAL_PARTIAL_RECORD:
        warnings.append(result.message or "Repaired an incomplete final session record")
    if result.logical is LogicalRecovery.INTERRUPTED_SAFE:
        await close_interrupted_turn(session=session, events=result.events)
        warnings.append("Previous process interruption was recorded; resuming the session")
        return PreparedResume(session_id, tuple(warnings))
    if result.logical is LogicalRecovery.UNCERTAIN_SIDE_EFFECT:
        if not allow_recovery_snapshot:
            raise SessionError(
                SessionErrorCode.CORRUPT_EVENT,
                "A write side effect is uncertain; use interactive /resume to create "
                "a safe recovery session",
            )
        recovered_id = await create_recovery_session(
            session=session,
            source_events=result.events,
            workspace_identity=workspace_identity,
        )
        warnings.append(
            "A write side effect is uncertain; preserved the original and created "
            f"recovery-{session_id[:8]}"
        )
        return PreparedResume(recovered_id, tuple(warnings))
    return PreparedResume(session_id, tuple(warnings))
