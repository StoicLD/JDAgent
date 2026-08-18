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


class ApprovalDecision(StrEnum):
    """A user's answer to an approval request."""

    APPROVE = "approve"
    REJECT = "reject"


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
