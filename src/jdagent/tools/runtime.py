"""Deep tool execution module: validation, permission, timeout, and normalization."""

import asyncio
import time
from collections.abc import Iterable

from jsonschema import Draft202012Validator, ValidationError

from jdagent.domain.events import (
    PermissionRequestedPayload,
    PermissionResolvedPayload,
    RuntimeEventType,
    ToolExecutionStartedPayload,
)
from jdagent.domain.tools import (
    ApprovalDecision,
    ApprovalRequest,
    PermissionDecision,
    ToolCall,
    ToolDefinition,
    ToolErrorCode,
    ToolExecutionContext,
    ToolResult,
    ToolResultStatus,
)
from jdagent.ports import ApprovalPort, RuntimeEventRecorder
from jdagent.tools.permissions import PermissionPolicy
from jdagent.tools.workspace import WorkspacePathError


class ToolRegistry:
    """Own the unique definition for each registered tool name."""

    def __init__(self, definitions: Iterable[ToolDefinition]) -> None:
        self._definitions: dict[str, ToolDefinition] = {}
        for definition in definitions:
            if definition.name in self._definitions:
                raise ValueError(f"Duplicate tool name: {definition.name}")
            self._definitions[definition.name] = definition

    def get(self, name: str) -> ToolDefinition | None:
        return self._definitions.get(name)

    def definitions(self) -> tuple[ToolDefinition, ...]:
        return tuple(self._definitions.values())


class ToolRuntime:
    """Execute one tool call while enforcing every pre-side-effect check."""

    def __init__(
        self,
        registry: ToolRegistry,
        policy: PermissionPolicy,
        approval: ApprovalPort,
        *,
        timeout_seconds: float = 10.0,
        recorder: RuntimeEventRecorder | None = None,
    ) -> None:
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        self._registry = registry
        self._policy = policy
        self._approval = approval
        self._timeout_seconds = timeout_seconds
        self._recorder = recorder

    async def execute(self, call: ToolCall, context: ToolExecutionContext) -> ToolResult:
        started = time.perf_counter()
        definition = self._registry.get(call.name)
        if definition is None:
            return self._error(call, ToolErrorCode.UNKNOWN_TOOL, "Unknown tool", started)

        try:
            Draft202012Validator(definition.input_schema).validate(call.arguments)  # pyright: ignore[reportUnknownMemberType]
        except ValidationError:
            return self._error(
                call,
                ToolErrorCode.INVALID_ARGUMENTS,
                "Arguments do not match the tool schema",
                started,
            )

        try:
            prepared_arguments = (
                definition.preflight(call.arguments, context)
                if definition.preflight is not None
                else dict(call.arguments)
            )
        except WorkspacePathError:
            return self._error(
                call,
                ToolErrorCode.PATH_OUTSIDE_WORKSPACE,
                "Requested path is not allowed",
                started,
            )
        except (TypeError, ValueError):
            return self._error(
                call,
                ToolErrorCode.INVALID_ARGUMENTS,
                "Arguments failed tool preflight validation",
                started,
            )

        prepared_call = ToolCall(call.call_id, call.name, prepared_arguments)
        decision = self._policy.decide(prepared_call, definition)
        if decision is PermissionDecision.DENY:
            await self._record_permission(context, call, decision)
            return self._error(
                call,
                ToolErrorCode.PERMISSION_DENIED,
                "Permission policy denied the tool call",
                started,
            )
        if decision is PermissionDecision.ASK:
            request = ApprovalRequest(call.name, prepared_arguments, definition.risk, call.call_id)
            if self._recorder is not None:
                await self._recorder.record(
                    context.turn_id,
                    RuntimeEventType.PERMISSION_REQUESTED,
                    PermissionRequestedPayload(request),
                )
            approval = await self._approval.request(request)
            await self._record_permission(context, call, decision, approval)
            if approval is ApprovalDecision.REJECT:
                return self._error(
                    call,
                    ToolErrorCode.APPROVAL_REJECTED,
                    "User rejected the tool call",
                    started,
                )

        else:
            await self._record_permission(context, call, decision)

        if self._recorder is not None:
            await self._recorder.record(
                context.turn_id,
                RuntimeEventType.TOOL_EXECUTION_STARTED,
                ToolExecutionStartedPayload(call.call_id, call.name),
            )
        try:
            output = await asyncio.wait_for(
                definition.handler(prepared_arguments, context),
                timeout=self._timeout_seconds,
            )
        except TimeoutError:
            return self._error(call, ToolErrorCode.TIMEOUT, "Tool execution timed out", started)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            return self._error(
                call,
                ToolErrorCode.EXECUTION_FAILED,
                f"{type(error).__name__}: tool handler failed",
                started,
            )
        return ToolResult(
            call.call_id,
            call.name,
            ToolResultStatus.SUCCESS,
            output=output,
            duration_ms=self._duration_ms(started),
        )

    async def _record_permission(
        self,
        context: ToolExecutionContext,
        call: ToolCall,
        policy: PermissionDecision,
        approval: ApprovalDecision | None = None,
    ) -> None:
        if self._recorder is not None:
            await self._recorder.record(
                context.turn_id,
                RuntimeEventType.PERMISSION_RESOLVED,
                PermissionResolvedPayload(call.call_id, policy, approval),
            )

    @classmethod
    def _error(
        cls,
        call: ToolCall,
        code: ToolErrorCode,
        message: str,
        started: float,
    ) -> ToolResult:
        return ToolResult(
            call.call_id,
            call.name,
            ToolResultStatus.ERROR,
            error_code=code,
            error_message=message,
            duration_ms=cls._duration_ms(started),
        )

    @staticmethod
    def _duration_ms(started: float) -> int:
        return max(0, round((time.perf_counter() - started) * 1000))
