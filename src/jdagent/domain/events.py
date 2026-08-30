"""Canonical runtime event schema version 2."""

from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import TypeAlias
from uuid import uuid4

from jdagent.domain.errors import StopReason
from jdagent.domain.model import ModelMessage, Usage
from jdagent.domain.tools import (
    ApprovalDecision,
    ApprovalRequest,
    PermissionDecision,
    RiskLevel,
    SessionPermissionRule,
    ToolCall,
    ToolResult,
)


class RuntimeEventType(StrEnum):
    """Durable event types required for recovery and audit."""

    SESSION_STARTED = "session_started"
    SESSION_RENAMED = "session_renamed"
    RECOVERY_SNAPSHOT = "recovery_snapshot"
    USER_MESSAGE = "user_message"
    TURN_RETRIEVAL_RECORDED = "turn_retrieval_recorded"
    ASSISTANT_MESSAGE_COMPLETED = "assistant_message_completed"
    TOOL_CALL_REQUESTED = "tool_call_requested"
    PERMISSION_REQUESTED = "permission_requested"
    PERMISSION_RESOLVED = "permission_resolved"
    PERMISSION_RULE_GRANTED = "permission_rule_granted"
    PERMISSION_RULE_REVOKED = "permission_rule_revoked"
    TOOL_EXECUTION_STARTED = "tool_execution_started"
    TOOL_EXECUTION_COMPLETED = "tool_execution_completed"
    MODEL_USAGE_RECORDED = "model_usage_recorded"
    TURN_COMPLETED = "turn_completed"
    TURN_FAILED = "turn_failed"


@dataclass(frozen=True, slots=True)
class SessionStartedPayload:
    """Marks the creation of a session."""

    name: str | None = None
    workspace_identity: str | None = None


@dataclass(frozen=True, slots=True)
class SessionRenamedPayload:
    """Changes the user-visible name of an existing session."""

    name: str


@dataclass(frozen=True, slots=True)
class RecoverySnapshotPayload:
    """Standalone context copied from a source session through a safe terminal event."""

    parent_session_id: str
    through_sequence: int
    messages: tuple[ModelMessage, ...]


@dataclass(frozen=True, slots=True)
class UserMessagePayload:
    """A user message accepted by the coordinator."""

    content: str


@dataclass(frozen=True, slots=True)
class CitationRecord:
    """A verified, turn-scoped citation attached to a completed assistant message."""

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
    """Frozen retrieval outcome recorded before the first model call."""

    outcome: str
    turn_token: str
    bases: tuple[RetrievalBaseRecord, ...] = ()
    evidence: tuple[RetrievalEvidenceRecord, ...] = ()
    budget: RetrievalBudgetRecord = RetrievalBudgetRecord(20, 20, 0, 40, 0, 0, 6000, 8)
    degradations: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class AssistantMessageCompletedPayload:
    """The final assistant content and tool calls for one model response."""

    content: str
    tool_calls: tuple[ToolCall, ...] = ()
    citations: tuple[CitationRecord, ...] = ()
    model_supplement: str = ""


@dataclass(frozen=True, slots=True)
class ToolCallRequestedPayload:
    """A complete tool call requested by a model."""

    call: ToolCall


@dataclass(frozen=True, slots=True)
class PermissionRequestedPayload:
    """An approval request emitted before a side effect."""

    request: ApprovalRequest


@dataclass(frozen=True, slots=True)
class PermissionResolvedPayload:
    """A policy or approval decision for a tool call."""

    call_id: str
    policy: PermissionDecision
    approval: ApprovalDecision | None = None


@dataclass(frozen=True, slots=True)
class PermissionRuleGrantedPayload:
    """Persists one active Session permission rule."""

    rule: SessionPermissionRule


@dataclass(frozen=True, slots=True)
class PermissionRuleRevokedPayload:
    """Deactivates one previously granted Session permission rule."""

    rule_id: str


@dataclass(frozen=True, slots=True)
class ToolExecutionStartedPayload:
    """Marks the beginning of an approved tool handler."""

    call_id: str
    tool_name: str
    risk: RiskLevel | None = None


@dataclass(frozen=True, slots=True)
class ToolExecutionCompletedPayload:
    """A normalized tool result."""

    result: ToolResult


@dataclass(frozen=True, slots=True)
class ModelUsageRecordedPayload:
    """Usage associated with one model call."""

    provider: str
    model: str
    usage: Usage


@dataclass(frozen=True, slots=True)
class TurnCompletedPayload:
    """A successfully terminated turn."""

    stop_reason: StopReason
    model_calls: int
    tool_calls: int
    provider: str = "unknown"
    model: str = "unknown"


@dataclass(frozen=True, slots=True)
class TurnFailedPayload:
    """A turn that ended without normal completion."""

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
    | TurnRetrievalRecordedPayload
    | AssistantMessageCompletedPayload
    | ToolCallRequestedPayload
    | PermissionRequestedPayload
    | PermissionResolvedPayload
    | PermissionRuleGrantedPayload
    | PermissionRuleRevokedPayload
    | ToolExecutionStartedPayload
    | ToolExecutionCompletedPayload
    | ModelUsageRecordedPayload
    | TurnCompletedPayload
    | TurnFailedPayload
)


@dataclass(frozen=True, slots=True)
class RuntimeEvent:
    """A canonical append-only runtime fact."""

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
        """Create a schema-v2 event with a unique ID and UTC timestamp."""

        return cls(
            schema_version=2,
            event_id=str(uuid4()),
            session_id=session_id,
            turn_id=turn_id,
            sequence=sequence,
            event_type=event_type,
            timestamp=datetime.now(UTC),
            payload=payload,
        )
