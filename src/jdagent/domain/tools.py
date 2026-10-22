"""Tool, permission, and approval domain types."""

import hashlib
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path

from jdagent.domain.json import JsonObject


def _empty_json_object() -> JsonObject:
    return {}


def command_log_id(call_id: str) -> str:
    """Derive the opaque log reference recoverable from a durable start event."""
    return hashlib.sha256(call_id.encode("utf-8")).hexdigest()


class RiskLevel(StrEnum):
    """Risk attached to a registered tool."""

    PURE = "pure"
    READ = "read"
    WRITE = "write"
    EXECUTE = "execute"


class ToolCapability(StrEnum):
    """Host-selected cumulative tool availability for one turn."""

    ANALYZE = "analyze"
    EDIT = "edit"
    EXECUTE = "execute"

    def allows(self, risk: RiskLevel) -> bool:
        if risk in (RiskLevel.PURE, RiskLevel.READ):
            return True
        if risk is RiskLevel.WRITE:
            return self is not ToolCapability.ANALYZE
        return self is ToolCapability.EXECUTE


class PermissionDecision(StrEnum):
    """A policy decision made before a tool side effect."""

    ALLOW = "allow"
    ASK = "ask"
    DENY = "deny"


class PermissionTargetKind(StrEnum):
    """The path scope of a persistent session permission rule."""

    FILE = "file"
    DIRECTORY = "directory"
    COMMAND = "command"
    MEMORY = "memory"


class ApprovalDecision(StrEnum):
    """A user's answer to an approval request."""

    APPROVE = "approve"
    REJECT = "reject"


@dataclass(frozen=True, slots=True)
class SessionPermissionRule:
    """A narrow, workspace-relative permission granted to one session."""

    rule_id: str
    session_id: str
    tool_name: str
    target_kind: PermissionTargetKind
    target: str
    args_prefix: tuple[str, ...] = ()
    cwd: str = ""
    environment_id: str = ""


@dataclass(frozen=True, slots=True)
class ApprovalOutcome:
    """One approval decision plus an optional persistent Session rule."""

    decision: ApprovalDecision
    granted_rule: SessionPermissionRule | None = None

    def __post_init__(self) -> None:
        if self.decision is ApprovalDecision.REJECT and self.granted_rule is not None:
            raise ValueError("A rejected approval cannot grant a permission rule")


class ToolResultStatus(StrEnum):
    """Whether a tool call completed successfully."""

    SUCCESS = "success"
    ERROR = "error"


class ToolErrorCode(StrEnum):
    """Stable failures at the tool seam."""

    UNKNOWN_TOOL = "unknown_tool"
    TOOL_UNAVAILABLE = "tool_unavailable"
    INVALID_ARGUMENTS = "invalid_arguments"
    PERMISSION_DENIED = "permission_denied"
    APPROVAL_REJECTED = "approval_rejected"
    PATH_OUTSIDE_WORKSPACE = "path_outside_workspace"
    TIMEOUT = "timeout"
    EXECUTION_FAILED = "execution_failed"
    CONFLICT = "conflict"
    NOT_EXECUTED = "not_executed"
    INSTRUCTIONS_DISCOVERED = "instructions_discovered"
    CANCELLED = "cancelled"
    UNCERTAIN = "uncertain"
    CAPACITY = "capacity"
    UNSUPPORTED = "unsupported"


@dataclass(frozen=True, slots=True)
class ToolOutcome:
    """Execution facts, independent of whether the user's task passed."""

    data: JsonObject = field(default_factory=_empty_json_object)
    completed: bool = True
    effect: str = "none"
    observed_failure: bool = False
    error_code: ToolErrorCode | None = None
    message: str = ""
    cancelled: bool = False
    requires_stop: bool = False

    def __post_init__(self) -> None:
        if self.effect not in {"none", "committed", "unknown", "possible"}:
            raise ValueError("Invalid effect state")

    @property
    def stop_turn(self) -> bool:
        return self.cancelled or self.effect == "unknown" or self.requires_stop

    def to_data(self) -> JsonObject:
        return {
            "data": self.data,
            "completed": self.completed,
            "effect": self.effect,
            "observed_failure": self.observed_failure,
            "error_code": self.error_code.value if self.error_code else None,
            "message": self.message,
            "cancelled": self.cancelled,
            "requires_stop": self.requires_stop,
        }


@dataclass(frozen=True, slots=True)
class ToolCall:
    """A complete model-requested tool invocation."""

    call_id: str
    name: str
    arguments: JsonObject


@dataclass(frozen=True, slots=True)
class ToolExecutionContext:
    """Host-controlled context made available to a tool handler."""

    session_id: str
    turn_id: str
    workspace: Path
    readable_log_sessions: tuple[str, ...] = ()
    call_id: str = ""
    memory_intent: str | None = None


ToolHandler = Callable[[JsonObject, ToolExecutionContext], Awaitable[str | ToolOutcome]]
ToolPreflight = Callable[[JsonObject, ToolExecutionContext], JsonObject]


@dataclass(frozen=True, slots=True)
class ToolDefinition:
    """The single registry record for one tool."""

    name: str
    description: str
    input_schema: JsonObject
    risk: RiskLevel
    handler: ToolHandler
    preflight: ToolPreflight | None = None
    result_schema: JsonObject | None = None
    manages_timeout: bool = False
    permission_target: PermissionTargetKind | None = None


@dataclass(frozen=True, slots=True)
class ToolResult:
    """A normalized tool execution outcome."""

    call_id: str
    tool_name: str
    status: ToolResultStatus
    output: str = ""
    error_code: ToolErrorCode | None = None
    error_message: str | None = None
    duration_ms: int = 0
    metadata: JsonObject = field(default_factory=_empty_json_object)
    outcome: ToolOutcome | None = None

    @property
    def stop_turn(self) -> bool:
        return self.outcome is not None and self.outcome.stop_turn


@dataclass(frozen=True, slots=True)
class ApprovalRequest:
    """Information required for a human approval decision."""

    tool_name: str
    arguments: JsonObject
    risk: RiskLevel
    call_id: str = ""
    session_id: str = ""
    target: str | None = None
    display: JsonObject = field(default_factory=_empty_json_object, repr=False)
