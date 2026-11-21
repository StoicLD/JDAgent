"""定义会话日志版本 4 的事件类型和载荷。一次外部操作可以产生多个事件，分别记录请求、开始和结果。"""

from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Literal, TypeAlias
from uuid import uuid4

from jdagent.domain.errors import StopReason
from jdagent.domain.mcp import MCPOperation, MCPTaskStatus, MCPTaskTiming
from jdagent.domain.model import Usage
from jdagent.domain.tools import (
    ApprovalDecision,
    ApprovalRequest,
    PermissionDecision,
    RiskLevel,
    SessionPermissionRule,
    ToolCall,
    ToolOutcome,
    ToolResult,
)


class RuntimeEventType(StrEnum):
    """区分会话日志中记录的事实类型，决定如何解释同一条记录的载荷。"""

    SESSION_STARTED = "session_started"
    SESSION_RENAMED = "session_renamed"
    RECOVERY_SNAPSHOT = "recovery_snapshot"
    USER_MESSAGE = "user_message"
    CONTEXT_WINDOW_SELECTED = "context_window_selected"
    MODEL_REQUEST_PREPARED = "model_request_prepared"
    COMPACTION_COMMITTED = "compaction_committed"
    COMPACTION_FAILED = "compaction_failed"
    CONSTRAINT_REVOKED = "constraint_revoked"
    SIDE_EFFECT_RESOLVED = "side_effect_resolved"
    TURN_RETRIEVAL_RECORDED = "turn_retrieval_recorded"
    ASSISTANT_MESSAGE_COMPLETED = "assistant_message_completed"
    TOOL_CALL_REQUESTED = "tool_call_requested"
    PERMISSION_REQUESTED = "permission_requested"
    PERMISSION_RESOLVED = "permission_resolved"
    PERMISSION_RULE_GRANTED = "permission_rule_granted"
    PERMISSION_RULE_REVOKED = "permission_rule_revoked"
    TOOL_EXECUTION_STARTED = "tool_execution_started"
    TOOL_EXECUTION_COMPLETED = "tool_execution_completed"
    MCP_INVOCATION_STARTED = "mcp_invocation_started"
    MCP_INVOCATION_FINISHED = "mcp_invocation_finished"
    MCP_ATTEMPT_FINISHED = "mcp_attempt_finished"
    MCP_TASK_REGISTERED = "mcp_task_registered"
    MCP_TASK_OBSERVED = "mcp_task_observed"
    MCP_TASK_CANCEL_REQUESTED = "mcp_task_cancel_requested"
    MCP_TASK_RESULT_SAVED = "mcp_task_result_saved"
    MCP_TASK_RESULT_RESERVED = "mcp_task_result_reserved"
    MCP_PROMPT_PREPARED = "mcp_prompt_prepared"
    MODEL_USAGE_RECORDED = "model_usage_recorded"
    TURN_COMPLETED = "turn_completed"
    TURN_FAILED = "turn_failed"


@dataclass(frozen=True, slots=True)
class SessionStartedPayload:
    """记录会话创建时的名称和工作区身份。"""

    name: str | None = None
    workspace_identity: str | None = None


@dataclass(frozen=True, slots=True)
class SessionRenamedPayload:
    """保存用户为已有会话指定的新名称。"""

    name: str


@dataclass(frozen=True, slots=True)
class RecoverySnapshotPayload:
    """引用父会话中经过校验的历史前缀，并列出中断后结果未知的操作。子会话不继承父会话的许可。"""

    parent_session_id: str
    through_sequence: int
    prefix_hash: str
    checkpoint_hash: str | None = None
    unknown_operations: tuple[str, ...] = ()
    gap_until: int = 0


@dataclass(frozen=True, slots=True)
class UserMessagePayload:
    """保存协调器已接受的用户原始输入。"""

    content: str


@dataclass(frozen=True, slots=True)
class ContextWindowSelectedPayload:
    """列出本次模型请求省略的历史轮次标识；原始事件仍保存在会话日志里。"""

    omitted_turn_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ModelRequestPreparedPayload:
    """在模型调用之前记录请求清单的位置，使事后能够核对模型输入。"""

    request_id: str
    object_hash: str
    call_kind: str
    input_tokens: int
    mcp_context: bool = False


@dataclass(frozen=True, slots=True)
class CompactionCommittedPayload:
    """确认新的压缩结果已经提交；后续请求据此选择摘要及其覆盖的历史范围。"""

    through_sequence: int
    prefix_hash: str
    object_hash: str


@dataclass(frozen=True, slots=True)
class CompactionFailedPayload:
    """标记压缩失败的历史前缀；历史未变化时，可据此抑制重复的自动压缩。"""

    through_sequence: int
    prefix_hash: str
    reason: str
    response_hashes: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ConstraintRevokedPayload:
    """将撤销动作关联到原约束的事件序号和本次用户指令。"""

    constraint_sequence: int
    user_event_id: str
    expected_version: int = 1


@dataclass(frozen=True, slots=True)
class SideEffectResolutionPayload:
    """记录用户对父会话某个未知操作的核实结论、依据和对应的用户消息。"""

    parent_session_id: str
    call_id: str
    user_event_id: str
    conclusion: str
    evidence: str


@dataclass(frozen=True, slots=True)
class CitationRecord:
    """将本轮已核验的引用连接到具体来源、版本和原文位置。"""

    ordinal: int
    evidence_id: str
    knowledge_base_id: str
    source_id: str
    source_version_id: str
    snapshot_id: str
    locator: str
    locator_schema_version: int
    content_hash: str


@dataclass(frozen=True, slots=True)
class RetrievalBaseRecord:
    """记录一次检索所用的知识库绑定、索引版本及该库的检索状态。"""

    binding_id: str
    connection_id: str
    knowledge_base_id: str
    kb_name: str
    status: str
    failure_reason: str | None
    generation_id: str | None
    revision: int
    physical_collection: str | None
    embedding_profile_fingerprint: str
    retrieval_profile_fingerprint: str
    hit_count: int
    selected_evidence_count: int


@dataclass(frozen=True, slots=True)
class RetrievalEvidenceRecord:
    """记录入选证据的顺序、内容摘要与原文定位，用于复核引用来源。"""

    evidence_id: str
    ordinal: int
    knowledge_base_id: str
    source_id: str
    source_version_id: str
    snapshot_id: str
    locator: str
    locator_schema_version: int
    content_hash: str


@dataclass(frozen=True, slots=True)
class RetrievalBudgetRecord:
    """记录检索、融合、重排和证据装入各阶段的数量及预算。"""

    dense_top_k: int
    bm25_top_k: int
    fused_child_count: int
    rerank_pool_size: int
    parent_count: int
    evidence_tokens: int
    evidence_token_limit: int
    parent_limit: int


@dataclass(frozen=True, slots=True)
class TurnRetrievalRecordedPayload:
    """在首次模型调用前固定本轮检索结果，使后续回答引用同一组证据。"""

    outcome: str
    turn_token: str
    bases: tuple[RetrievalBaseRecord, ...] = ()
    evidence: tuple[RetrievalEvidenceRecord, ...] = ()
    budget: RetrievalBudgetRecord = RetrievalBudgetRecord(20, 20, 0, 40, 0, 0, 6000, 8)
    degradations: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class AssistantMessageCompletedPayload:
    """保存一次完整模型响应中的文本和工具调用；同一轮对话可以有多条此类消息。"""

    content: str
    tool_calls: tuple[ToolCall, ...] = ()
    citations: tuple[CitationRecord, ...] = ()
    model_supplement: str = ""


@dataclass(frozen=True, slots=True)
class ToolCallRequestedPayload:
    """记录模型已经给出完整参数的一次工具请求，供后续按调用标识配对。"""

    call: ToolCall


@dataclass(frozen=True, slots=True)
class PermissionRequestedPayload:
    """记录执行副作用前向用户提出的审批请求。"""

    request: ApprovalRequest


@dataclass(frozen=True, slots=True)
class PermissionResolvedPayload:
    """记录策略或用户对某次调用作出的权限决定。"""

    call_id: str
    policy: PermissionDecision
    approval: ApprovalDecision | None = None


@dataclass(frozen=True, slots=True)
class PermissionRuleGrantedPayload:
    """记录新增的会话许可及其适用范围。"""

    rule: SessionPermissionRule


@dataclass(frozen=True, slots=True)
class PermissionRuleRevokedPayload:
    """标记指定的会话许可已被撤销。"""

    rule_id: str


@dataclass(frozen=True, slots=True)
class ToolExecutionStartedPayload:
    """标记工具即将进入实际执行阶段；缺少对应结果时，恢复流程必须考虑副作用可能已经发生。"""

    call_id: str
    tool_name: str
    risk: RiskLevel | None = None


@dataclass(frozen=True, slots=True)
class ToolExecutionCompletedPayload:
    """保存与指定工具调用配对的统一结果，供模型继续处理及会话恢复。"""

    result: ToolResult


@dataclass(frozen=True, slots=True)
class MCPInvocationStartedPayload:
    """在可能发送请求前持久化调用意图和容量预留，使中断后仍能核查本次操作。"""

    call_id: str
    server_id: str
    raw_name: str
    generation: str
    request_id: str
    arguments_sha256: str
    reserved_bytes: int
    attempt: int = 1
    operation: MCPOperation = "tools/call"


@dataclass(frozen=True, slots=True)
class MCPInvocationFinishedPayload:
    """记录一次 MCP 调用的结束状态，区分已确认的结果和仍未确认的结果，可关联归档原文。"""

    result: ToolResult
    result_confirmed: bool
    original_hash: str | None = None
    original_bytes: int = 0


@dataclass(frozen=True, slots=True)
class MCPAttemptFinishedPayload:
    """保存一次协议尝试的脱敏结果；一次业务操作可能包含多次尝试。"""

    call_id: str
    attempt: int
    response_confirmed: bool
    failure_kind: str | None = None
    rpc_code: int | None = None


@dataclass(frozen=True, slots=True)
class MCPTaskRegisteredPayload:
    """记录远端任务与本地受控对象的关联，首次查询前必须能从该对象找回任务标识。"""

    call_id: str
    task_ref: str
    server_id: str
    raw_name: str
    generation: str
    handle_hash: str
    status: MCPTaskStatus
    handle_bytes: int
    timing: MCPTaskTiming | None = None


@dataclass(frozen=True, slots=True)
class MCPTaskObservedPayload:
    """保存一次任务状态观察，供重启后的查询与结果恢复使用。"""

    call_id: str
    task_ref: str
    status: MCPTaskStatus | None
    attempt: int = 1
    response_confirmed: bool = True
    failure_kind: str | None = None
    rpc_code: int | None = None
    operation: Literal["get", "cancel"] = "get"
    timing: MCPTaskTiming | None = None


@dataclass(frozen=True, slots=True)
class MCPTaskCancelRequestedPayload:
    """记录已请求远端取消任务；是否真正停止需以后续观察为准。"""

    call_id: str
    task_ref: str


@dataclass(frozen=True, slots=True)
class MCPTaskResultReservedPayload:
    """记录为重新读取原任务结果而预留的持久存储额度。"""

    task_ref: str
    call_id: str
    origin_turn_id: str
    reserved_bytes: int


@dataclass(frozen=True, slots=True)
class MCPTaskResultSavedPayload:
    """记录显式管理命令取回并保存的任务结果，与原模型调用的完成事件分别保存。"""

    task_ref: str
    call_id: str
    origin_turn_id: str
    outcome: ToolOutcome
    original_hash: str
    original_bytes: int


@dataclass(frozen=True, slots=True)
class MCPPromptPreparedPayload:
    """记录宿主显式从 MCP 服务读取的提示材料，保留其外部来源。"""

    call_id: str
    outcome: ToolOutcome


@dataclass(frozen=True, slots=True)
class ModelUsageRecordedPayload:
    """记录模型服务报告的一次调用用量，并关联请求及调用用途。"""

    provider: str
    model: str
    usage: Usage
    request_id: str | None = None
    call_kind: str = "task"


@dataclass(frozen=True, slots=True)
class TurnCompletedPayload:
    """保存本轮正常完成的停止原因以及模型和工具调用次数。"""

    stop_reason: StopReason
    model_calls: int
    tool_calls: int
    provider: str = "unknown"
    model: str = "unknown"


@dataclass(frozen=True, slots=True)
class TurnFailedPayload:
    """保存本轮未能正常完成的原因、错误类别及已用调用次数。"""

    stop_reason: StopReason
    error_category: str
    message: str
    model_calls: int
    tool_calls: int
    provider: str = "unknown"
    model: str = "unknown"


RuntimePayload: TypeAlias = (
    SessionStartedPayload
    | SessionRenamedPayload
    | RecoverySnapshotPayload
    | UserMessagePayload
    | ContextWindowSelectedPayload
    | ModelRequestPreparedPayload
    | CompactionCommittedPayload
    | CompactionFailedPayload
    | ConstraintRevokedPayload
    | SideEffectResolutionPayload
    | TurnRetrievalRecordedPayload
    | AssistantMessageCompletedPayload
    | ToolCallRequestedPayload
    | PermissionRequestedPayload
    | PermissionResolvedPayload
    | PermissionRuleGrantedPayload
    | PermissionRuleRevokedPayload
    | ToolExecutionStartedPayload
    | ToolExecutionCompletedPayload
    | MCPInvocationStartedPayload
    | MCPInvocationFinishedPayload
    | MCPAttemptFinishedPayload
    | MCPTaskRegisteredPayload
    | MCPTaskObservedPayload
    | MCPTaskCancelRequestedPayload
    | MCPTaskResultSavedPayload
    | MCPTaskResultReservedPayload
    | MCPPromptPreparedPayload
    | ModelUsageRecordedPayload
    | TurnCompletedPayload
    | TurnFailedPayload
)


@dataclass(frozen=True, slots=True)
class RuntimeEvent:
    """会话追加日志中的一条事实记录。会话内的序号给出顺序，轮次标识用于归集同一轮发生的事件。"""

    schema_version: int
    event_id: str
    session_id: str
    turn_id: str | None
    sequence: int
    event_type: RuntimeEventType
    timestamp: datetime
    payload: RuntimePayload

    @classmethod
    def create(
        cls,
        *,
        session_id: str,
        turn_id: str | None,
        sequence: int,
        event_type: RuntimeEventType,
        payload: RuntimePayload,
    ) -> "RuntimeEvent":
        """生成版本 4 的事件，分配唯一标识和 UTC 时间，沿用调用方提供的会话内序号。"""

        return cls(
            schema_version=4,
            event_id=str(uuid4()),
            session_id=session_id,
            turn_id=turn_id,
            sequence=sequence,
            event_type=event_type,
            timestamp=datetime.now(UTC),
            payload=payload,
        )
