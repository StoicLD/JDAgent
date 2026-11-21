"""描述宿主管理的 MCP 连接、能力和状态，可独立于协议 SDK 使用。"""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Literal, TypeAlias, cast

from jdagent.domain.json import JsonObject, require_string

MCPConnectionState = Literal["not_connected", "connecting", "ready", "unavailable", "closed"]
MCPProgressCallback: TypeAlias = Callable[[float, float | None], Awaitable[None]]
MCPMethod = Literal[
    "tools/list",
    "resources/list",
    "resources/templates/list",
    "prompts/list",
    "tools/call",
    "resources/read",
    "prompts/get",
    "tasks/get",
    "tasks/cancel",
]
MCPOperation = Literal["tools/call", "resources/read", "prompts/get", "discovery"]
MCPCapabilityKind = Literal["tool", "resource", "resource_template", "prompt"]
MCPDirectoryState = Literal["not_loaded", "ready", "partial", "failed", "evicted", "disabled"]
MCPTaskStatus = Literal["working", "input_required", "completed", "failed", "cancelled"]
MCPFailureKind = Literal[
    "configuration",
    "unsupported",
    "connection",
    "connection_timeout",
    "protocol",
    "remote",
    "server_timeout",
    "busy",
    "response_limit",
    "directory_capacity",
    "identity_mismatch",
    "task_expired",
    "task_unavailable",
    "cleanup_timeout",
    "interrupted",
]


@dataclass(frozen=True, slots=True)
class MCPPeerInformation:
    """保存握手协商出的协议版本、服务能力及用于核验服务身份的摘要。"""

    protocol_version: str
    tools: bool
    resources: bool
    prompts: bool
    tasks: bool
    identity_digest: str = field(default="", repr=False)


@dataclass(frozen=True, slots=True)
class MCPTaskTiming:
    """记录远端任务的创建时间、保留时间与建议轮询间隔。宿主等待本轮操作的时限单独计算。"""

    created_at: str
    ttl_ms: int | None
    poll_interval_ms: int | None

    @classmethod
    def from_data(cls, data: JsonObject) -> "MCPTaskTiming":
        created_at = require_string(data, "createdAt")
        if datetime.fromisoformat(created_at).tzinfo is None:
            raise ValueError("Task creation timestamp must include a timezone")
        ttl, interval = data.get("ttlMs"), data.get("pollIntervalMs")
        if any(
            type(value) is not int or value < 0 for value in (ttl, interval) if value is not None
        ):
            raise ValueError("Invalid Task retention or polling interval")
        return cls(created_at, cast(int | None, ttl), cast(int | None, interval))

    def to_data(self) -> JsonObject:
        return {
            "createdAt": self.created_at,
            "ttlMs": self.ttl_ms,
            "pollIntervalMs": self.poll_interval_ms,
        }

    @property
    def expired(self) -> bool:
        elapsed = (datetime.now(UTC) - datetime.fromisoformat(self.created_at)).total_seconds()
        return self.ttl_ms is not None and elapsed * 1000 >= self.ttl_ms


@dataclass(frozen=True, slots=True)
class MCPTask:
    """保存经过校验的远端任务标识和状态。远端标识供宿主查询，不能作为普通模型材料公开。"""

    task_id: str = field(repr=False)
    status: MCPTaskStatus
    created_at: str
    ttl_ms: int | None
    poll_interval_ms: int | None

    @classmethod
    def from_data(cls, data: JsonObject) -> "MCPTask":
        status = data.get("status")
        if status not in ("working", "input_required", "completed", "failed", "cancelled"):
            raise MCPFailure("protocol")
        try:
            timing = MCPTaskTiming.from_data(data)
        except ValueError:
            raise MCPFailure("protocol") from None
        return cls(
            require_string(data, "taskId"),
            status,
            timing.created_at,
            timing.ttl_ms,
            timing.poll_interval_ms,
        )

    @property
    def timing(self) -> MCPTaskTiming:
        return MCPTaskTiming(self.created_at, self.ttl_ms, self.poll_interval_ms)


@dataclass(frozen=True, slots=True)
class MCPTaskSnapshot:
    """将一次查询得到的远端任务状态与可选结果关联起来。"""

    task: MCPTask
    result: JsonObject | None


@dataclass(frozen=True, slots=True)
class MCPTaskView:
    """供本地界面展示的任务状态，使用本地引用并标明结果是否已可用。"""

    task_ref: str
    server_id: str
    raw_name: str
    status: MCPTaskStatus
    result_available: bool
    reference_expired: bool | None = None


class MCPFailure(Exception):
    """表示经过脱敏的连接或协议错误。能否重试还取决于业务请求是否可能已经发送。"""

    def __init__(self, kind: MCPFailureKind, *, rpc_code: int | None = None) -> None:
        self.kind: MCPFailureKind = kind
        self.rpc_code = rpc_code
        super().__init__(f"MCP operation failed: {kind}")


@dataclass(frozen=True, slots=True)
class MCPCapability:
    """表示指定服务和目录版本下发布的一项能力，包含能力类型、名称及定义。"""

    server_id: str
    generation: str
    kind: MCPCapabilityKind
    name: str
    description: str
    definition_json: str


@dataclass(frozen=True, slots=True)
class MCPRejectedCapability:
    """记录被目录校验拒绝的单个条目和理由，其他有效条目仍可使用。"""

    name: str
    reason: str
    kind: MCPCapabilityKind = "tool"


@dataclass(frozen=True, slots=True)
class MCPActivityStatus:
    """保存宿主观察到的进度、已用时间和任务状态；进度通知不延长操作期限。"""

    name: str
    operation: str
    elapsed_seconds: float
    progress: float | None = None
    total: float | None = None
    task_status: MCPTaskStatus | None = None


@dataclass(frozen=True, slots=True)
class MCPServerStatus:
    """汇总单个服务的连接状态、目录完整性、当前活动与缓存用量。"""

    server_id: str
    generation: str | None
    connection: MCPConnectionState
    directory: MCPDirectoryState
    protocol_version: str | None = None
    reason: str | None = None
    entry_count: int = 0
    metadata_bytes: int = 0
    index_bytes: int = 0
    rejected: tuple[MCPRejectedCapability, ...] = ()
    activity: MCPActivityStatus | None = None


@dataclass(frozen=True, slots=True)
class MCPDirectoryLimits:
    """限定会话内目录的条目数、内存计量与分页数，也约束装载中的单个服务目录。"""

    entries: int = 2000
    metadata_bytes: int = 8 << 20
    index_bytes: int = 4 << 20
    pages_per_kind: int = 20

    def __post_init__(self) -> None:
        if any(
            value <= 0
            for value in (self.entries, self.metadata_bytes, self.index_bytes, self.pages_per_kind)
        ):
            raise ValueError("MCP directory limits must be positive")


@dataclass(frozen=True, slots=True)
class MCPCatalogView:
    """将可用条目与各服务的加载状态一起返回，调用方据此判断目录是否完整。"""

    entries: tuple[MCPCapability, ...]
    servers: tuple[MCPServerStatus, ...]

    @property
    def complete(self) -> bool:
        return all(server.directory in {"ready", "disabled"} for server in self.servers)


@dataclass(frozen=True, slots=True)
class MCPSearchHit:
    """将一次能力检索命中的条目与相关性得分关联。"""

    capability: MCPCapability
    score: float


@dataclass(frozen=True, slots=True)
class MCPSearchResult:
    """保存本页命中、下一页位置及服务状态，使调用方能识别目录只加载了部分的情况。"""

    hits: tuple[MCPSearchHit, ...]
    servers: tuple[MCPServerStatus, ...]
    next_offset: int | None
    suggestion: str | None = None

    @property
    def complete(self) -> bool:
        return all(server.directory in {"ready", "disabled"} for server in self.servers)
