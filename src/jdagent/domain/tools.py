"""定义工具、权限判断和用户授权的公共数据类型。"""

import hashlib
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Literal

from jdagent.domain.json import JsonObject


def _empty_json_object() -> JsonObject:
    return {}


def command_log_id(call_id: str) -> str:
    """从工具调用标识生成命令日志引用，恢复时可根据已保存的开始事件重新计算。"""
    return hashlib.sha256(call_id.encode("utf-8")).hexdigest()


class RiskLevel(StrEnum):
    """标记宿主注册工具时指定的风险级别，供后续策略判断使用。"""

    PURE = "pure"
    READ = "read"
    WRITE = "write"
    EXECUTE = "execute"


class ToolCapability(StrEnum):
    """定义本轮允许使用的工具能力范围，较高范围包含较低范围的能力。"""

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
    """宿主在产生副作用之前作出的策略判断，区分放行、询问和拒绝。"""

    ALLOW = "allow"
    ASK = "ask"
    DENY = "deny"


class PermissionTargetKind(StrEnum):
    """描述会话许可匹配操作目标时使用的范围类型。"""

    FILE = "file"
    DIRECTORY = "directory"
    COMMAND = "command"
    MEMORY = "memory"


class ApprovalDecision(StrEnum):
    """表示用户针对具体审批请求作出的决定。"""

    APPROVE = "approve"
    REJECT = "reject"


@dataclass(frozen=True, slots=True)
class SessionPermissionRule:
    """保存仅对当前会话有效的有限授权，按工具和操作目标匹配后续请求。"""

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
    """包含本次审批决定，以及用户选择持续授权时生成的会话规则。"""

    decision: ApprovalDecision
    granted_rule: SessionPermissionRule | None = None

    def __post_init__(self) -> None:
        if self.decision is ApprovalDecision.REJECT and self.granted_rule is not None:
            raise ValueError("A rejected approval cannot grant a permission rule")


class ToolResultStatus(StrEnum):
    """标记一次工具调用的结果状态，供模型和界面读取。"""

    SUCCESS = "success"
    ERROR = "error"


class ToolErrorCode(StrEnum):
    """将工具错误归入稳定类别，使调用方能区分权限、参数、容量和执行故障。"""

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
    """保存实际观察到的执行事实，可用于统计失败次数和决定是否继续。"""

    data: JsonObject = field(default_factory=_empty_json_object)
    completed: bool = True
    effect: str = "none"
    observed_failure: bool = False
    error_code: ToolErrorCode | None = None
    message: str = ""
    cancelled: bool = False
    requires_stop: bool = False
    derived_from_mcp: bool = False

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
            **({"derived_from_mcp": True} if self.derived_from_mcp else {}),
        }


@dataclass(frozen=True, slots=True)
class ToolCall:
    """模型提交的一次完整工具请求，包含调用标识、工具名称与参数。"""

    call_id: str
    name: str
    arguments: JsonObject


@dataclass(frozen=True, slots=True)
class ToolExecutionContext:
    """宿主提供的执行上下文，限定工具可使用的工作区、会话、轮次及相关授权信息。"""

    session_id: str
    turn_id: str
    workspace: Path
    readable_log_sessions: tuple[str, ...] = ()
    call_id: str = ""
    memory_intent: str | None = None
    request_id: str = ""
    external_context: bool = False
    tool_name: str = ""


ToolHandler = Callable[[JsonObject, ToolExecutionContext], Awaitable[str | ToolOutcome]]
ToolPreflight = Callable[[JsonObject, ToolExecutionContext], JsonObject]


@dataclass(frozen=True, slots=True)
class ToolOrigin:
    """工具注册时由宿主确定的来源，用于区分本地和外部工具，调用参数不能改写它。"""

    kind: Literal["builtin", "mcp", "mcp_control"] = "builtin"
    server_id: str | None = None
    raw_name: str | None = None
    generation: str | None = None
    definition_sha256: str | None = None
    capability_kind: Literal["tool", "resource", "prompt"] | None = None


@dataclass(frozen=True, slots=True)
class ToolDefinition:
    """工具在宿主注册表中的定义，把模型可见的参数结构与实际处理函数、权限属性关联起来。"""

    name: str
    description: str
    input_schema: JsonObject
    risk: RiskLevel
    handler: ToolHandler
    preflight: ToolPreflight | None = None
    result_schema: JsonObject | None = None
    manages_timeout: bool = False
    permission_target: PermissionTargetKind | None = None
    origin: ToolOrigin = field(default_factory=ToolOrigin)


@dataclass(frozen=True, slots=True)
class ToolResult:
    """对一次工具调用的统一答复，通过调用标识与请求配对，可携带输出、错误和执行事实。"""

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
    """供用户判断是否授权的一次操作说明，包括工具名称与本次实际操作的目标。"""

    tool_name: str
    arguments: JsonObject
    risk: RiskLevel
    call_id: str = ""
    session_id: str = ""
    target: str | None = None
    display: JsonObject = field(default_factory=_empty_json_object, repr=False)
