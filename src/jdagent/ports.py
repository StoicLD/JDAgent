"""Dependency-inversion seams consumed by the runtime."""

from collections.abc import AsyncIterator, Sequence
from typing import Protocol

from jdagent.domain.events import RuntimeEvent, RuntimeEventType, RuntimePayload
from jdagent.domain.json import JsonObject
from jdagent.domain.mcp import (
    MCPConnectionState,
    MCPMethod,
    MCPPeerInformation,
    MCPProgressCallback,
)
from jdagent.domain.model import ModelCapabilities, ModelEvent, ModelRequest
from jdagent.domain.tools import (
    ApprovalOutcome,
    ApprovalRequest,
    ToolCall,
    ToolDefinition,
    ToolExecutionContext,
    ToolResult,
)


class ModelPort(Protocol):
    """Streams provider-independent events for one model request."""

    @property
    def capabilities(self) -> ModelCapabilities: ...

    def stream(self, request: ModelRequest) -> AsyncIterator[ModelEvent]: ...


class MCPConnectionPort(Protocol):
    """A Session-owned peer; all protocol types and secrets remain in its adapter."""

    @property
    def state(self) -> MCPConnectionState: ...

    @property
    def information(self) -> MCPPeerInformation | None: ...

    async def connect(self) -> MCPPeerInformation: ...

    def input_schema_issue(self, schema: JsonObject) -> str | None:
        """Report a negotiated-protocol constraint before exposing a tool."""
        ...

    async def request(
        self,
        method: MCPMethod,
        params: JsonObject,
        *,
        on_progress: MCPProgressCallback | None = None,
        input_schema: JsonObject | None = None,
    ) -> JsonObject: ...

    async def aclose(self) -> bool: ...


class SessionPort(Protocol):
    """Appends and reads canonical events for a session."""

    async def append(self, event: RuntimeEvent) -> None: ...

    def read(self, session_id: str) -> AsyncIterator[RuntimeEvent]: ...

    async def acquire(self, session_id: str) -> None: ...

    async def release(self, session_id: str) -> None: ...


class ContextObjects(Protocol):
    """Persist and verify immutable content before publishing its hash in an event."""

    async def put(self, content: bytes) -> str: ...

    async def get(self, digest: str) -> bytes: ...


class SessionDiscoveryPort(Protocol):
    """Lists identifiers without exposing storage layout to catalog use cases."""

    async def list_session_ids(self) -> Sequence[str]: ...


class ToolRuntimePort(Protocol):
    """Validates, authorizes, and executes one complete tool call."""

    async def execute(self, call: ToolCall, context: ToolExecutionContext) -> ToolResult: ...


class DynamicToolsPort(Protocol):
    """Prepare definitions only between requests; check live host authorization at execution."""

    async def prepare_tools(self) -> tuple[ToolDefinition, ...]: ...

    def allowed(self, definition: ToolDefinition) -> bool: ...

    def used(self, definition: ToolDefinition) -> None: ...

    def unload(self, definition: ToolDefinition) -> None: ...


class RequestToolsPort(Protocol):
    """Freeze the exact definitions projected into an admitted model request."""

    async def prepare_request(self) -> tuple[ToolDefinition, ...]: ...

    def reduce_tools(self, request: ModelRequest) -> ModelRequest: ...

    def bind_request(self, request: ModelRequest) -> ModelRequest: ...


class ApprovalPort(Protocol):
    """Collects a human answer for an ASK policy decision."""

    async def request(self, request: ApprovalRequest) -> ApprovalOutcome: ...


class RuntimeEventSink(Protocol):
    """Persists a canonical event before notifying observers."""

    async def emit(self, event: RuntimeEvent) -> None: ...


class EventObserver(Protocol):
    """Consume persisted runtime facts without changing recovery state."""

    async def observe(self, event: RuntimeEvent) -> None: ...


class RuntimeEventRecorder(Protocol):
    """Create and persist the next canonical event for one session."""

    async def record(
        self,
        turn_id: str | None,
        event_type: RuntimeEventType,
        payload: RuntimePayload,
    ) -> RuntimeEvent: ...


class RuntimeJournal(RuntimeEventRecorder, Protocol):
    """Expose immutable history and append new facts at the Core seam."""

    @property
    def events(self) -> Sequence[RuntimeEvent]: ...
