"""Dependency-inversion seams consumed by the runtime."""

from collections.abc import AsyncIterator, Sequence
from typing import Protocol

from jdagent.domain.events import RuntimeEvent, RuntimeEventType, RuntimePayload
from jdagent.domain.model import ModelCapabilities, ModelEvent, ModelRequest
from jdagent.domain.tools import (
    ApprovalDecision,
    ApprovalRequest,
    ToolCall,
    ToolExecutionContext,
    ToolResult,
)


class ModelPort(Protocol):
    """Streams provider-independent events for one model request."""

    @property
    def capabilities(self) -> ModelCapabilities: ...

    def stream(self, request: ModelRequest) -> AsyncIterator[ModelEvent]: ...


class SessionPort(Protocol):
    """Appends and reads canonical events for a session."""

    async def append(self, event: RuntimeEvent) -> None: ...

    def read(self, session_id: str) -> AsyncIterator[RuntimeEvent]: ...


class ToolRuntimePort(Protocol):
    """Validates, authorizes, and executes one complete tool call."""

    async def execute(self, call: ToolCall, context: ToolExecutionContext) -> ToolResult: ...


class ApprovalPort(Protocol):
    """Collects a human answer for an ASK policy decision."""

    async def request(self, request: ApprovalRequest) -> ApprovalDecision: ...


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
