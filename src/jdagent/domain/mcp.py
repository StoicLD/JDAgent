"""Host-owned MCP connection state, without SDK objects or credential values."""

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
    protocol_version: str
    tools: bool
    resources: bool
    prompts: bool
    tasks: bool
    identity_digest: str = field(default="", repr=False)


@dataclass(frozen=True, slots=True)
class MCPTaskTiming:
    """Server retention metadata, independent of the local execution deadline."""

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
    """A validated remote handle, never serialized as ordinary model material."""

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
    task: MCPTask
    result: JsonObject | None


@dataclass(frozen=True, slots=True)
class MCPTaskView:
    """Public, local-only task status; never contains the remote handle."""

    task_ref: str
    server_id: str
    raw_name: str
    status: MCPTaskStatus
    result_available: bool
    reference_expired: bool | None = None


class MCPFailure(Exception):
    """A sanitized boundary failure; an RPC code alone never authorizes replay."""

    def __init__(self, kind: MCPFailureKind, *, rpc_code: int | None = None) -> None:
        self.kind: MCPFailureKind = kind
        self.rpc_code = rpc_code
        super().__init__(f"MCP operation failed: {kind}")


@dataclass(frozen=True, slots=True)
class MCPCapability:
    server_id: str
    generation: str
    kind: MCPCapabilityKind
    name: str
    description: str
    definition_json: str


@dataclass(frozen=True, slots=True)
class MCPRejectedCapability:
    name: str
    reason: str
    kind: MCPCapabilityKind = "tool"


@dataclass(frozen=True, slots=True)
class MCPActivityStatus:
    """A bounded local observation; progress never changes an operation's deadline."""

    name: str
    operation: str
    elapsed_seconds: float
    progress: float | None = None
    total: float | None = None
    task_status: MCPTaskStatus | None = None


@dataclass(frozen=True, slots=True)
class MCPServerStatus:
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
    """Per-Session cache accounting, also bounding one staged Server directory."""

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
    entries: tuple[MCPCapability, ...]
    servers: tuple[MCPServerStatus, ...]

    @property
    def complete(self) -> bool:
        return all(server.directory in {"ready", "disabled"} for server in self.servers)


@dataclass(frozen=True, slots=True)
class MCPSearchHit:
    capability: MCPCapability
    score: float


@dataclass(frozen=True, slots=True)
class MCPSearchResult:
    hits: tuple[MCPSearchHit, ...]
    servers: tuple[MCPServerStatus, ...]
    next_offset: int | None
    suggestion: str | None = None

    @property
    def complete(self) -> bool:
        return all(server.directory in {"ready", "disabled"} for server in self.servers)
