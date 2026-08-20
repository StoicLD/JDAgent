"""Tool, permission, and approval domain types."""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path

from jdagent.domain.json import JsonObject


def _empty_json_object() -> JsonObject:
    return {}


class RiskLevel(StrEnum):
    """Risk attached to a registered tool."""

    PURE = "pure"
    READ = "read"
    WRITE = "write"


class PermissionDecision(StrEnum):
    """A policy decision made before a tool side effect."""

    ALLOW = "allow"
    ASK = "ask"
    DENY = "deny"


class PermissionTargetKind(StrEnum):
    """The path scope of a persistent session permission rule."""

    FILE = "file"
    DIRECTORY = "directory"


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
    INVALID_ARGUMENTS = "invalid_arguments"
    PERMISSION_DENIED = "permission_denied"
    APPROVAL_REJECTED = "approval_rejected"
    PATH_OUTSIDE_WORKSPACE = "path_outside_workspace"
    TIMEOUT = "timeout"
    EXECUTION_FAILED = "execution_failed"


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


ToolHandler = Callable[[JsonObject, ToolExecutionContext], Awaitable[str]]
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


@dataclass(frozen=True, slots=True)
class ApprovalRequest:
    """Information required for a human approval decision."""

    tool_name: str
    arguments: JsonObject
    risk: RiskLevel
    call_id: str = ""
    session_id: str = ""
    target: str | None = None
