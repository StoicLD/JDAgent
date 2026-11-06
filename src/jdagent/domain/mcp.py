"""Host-owned MCP connection state, without SDK objects or credential values."""

from dataclasses import dataclass
from typing import Literal

MCPConnectionState = Literal["not_connected", "connecting", "ready", "unavailable", "closed"]
MCPMethod = Literal[
    "tools/list",
    "resources/list",
    "resources/templates/list",
    "prompts/list",
    "tools/call",
    "resources/read",
    "prompts/get",
]
MCPOperation = Literal["tools/call", "resources/read", "prompts/get", "discovery"]
MCPCapabilityKind = Literal["tool", "resource", "resource_template", "prompt"]
MCPDirectoryState = Literal["not_loaded", "ready", "partial", "failed", "evicted", "disabled"]
MCPFailureKind = Literal[
    "configuration",
    "unsupported",
    "connection",
    "connection_timeout",
    "protocol",
    "remote",
    "busy",
    "response_limit",
    "directory_capacity",
]


@dataclass(frozen=True, slots=True)
class MCPPeerInformation:
    protocol_version: str
    tools: bool
    resources: bool
    prompts: bool
    tasks: bool


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
