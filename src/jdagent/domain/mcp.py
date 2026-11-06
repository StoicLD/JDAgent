"""Host-owned MCP connection state, without SDK objects or credential values."""

from dataclasses import dataclass
from typing import Literal

MCPConnectionState = Literal["not_connected", "connecting", "ready", "unavailable", "closed"]
MCPFailureKind = Literal[
    "configuration",
    "unsupported",
    "connection",
    "connection_timeout",
    "protocol",
    "remote",
    "busy",
    "response_limit",
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
