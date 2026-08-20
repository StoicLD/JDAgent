"""Deep tool execution module: validation, permission, timeout, and normalization."""

import asyncio
import time
from collections.abc import Iterable
from pathlib import Path

from jsonschema import Draft202012Validator, ValidationError

from jdagent.domain.events import (
    PermissionRequestedPayload,
    PermissionResolvedPayload,
    PermissionRuleGrantedPayload,
    RuntimeEventType,
    ToolExecutionStartedPayload,
)
from jdagent.domain.json import JsonObject
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
from jdagent.tools.permissions import PermissionPolicy, SessionPermissionPolicy
from jdagent.tools.workspace import WorkspacePathError


def _approval_argument_summary(
    arguments: JsonObject,
    *,
    target: str | None,
) -> JsonObject:
    """Describe argument shape without retaining user content or secret values."""

    summary: JsonObject = {}
    for name in sorted(arguments):
        value = arguments[name]
        if name == "path" and target is not None:
            summary[name] = target
        elif isinstance(value, str):
            summary[name] = f"<{len(value)} chars>"
        elif isinstance(value, list):
            summary[name] = f"<{len(value)} items>"
        elif isinstance(value, dict):
            summary[name] = f"<{len(value)} fields>"
        else:
            summary[name] = f"<{type(value).__name__}>"
    return summary


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
            prepared_path = prepared_arguments.get("path")
            target: str | None = None
            if isinstance(prepared_path, str):
                try:
                    target = (
                        Path(prepared_path)
                        .relative_to(context.workspace.resolve(strict=True))
                        .as_posix()
                    )
                except (OSError, ValueError):
                    return self._error(
                        call,
                        ToolErrorCode.PATH_OUTSIDE_WORKSPACE,
                        "Approved path could not be normalized",
                        started,
                    )
            request = ApprovalRequest(
                call.name,
                _approval_argument_summary(prepared_arguments, target=target),
                definition.risk,
                call.call_id,
                context.session_id,
                target,
            )
            if self._recorder is not None:
                await self._recorder.record(
                    context.turn_id,
                    RuntimeEventType.PERMISSION_REQUESTED,
                    PermissionRequestedPayload(request),
                )
            outcome = await self._approval.request(request)
            if outcome.granted_rule is not None:
                verification = SessionPermissionPolicy(
                    workspace=context.workspace,
                    session_id=context.session_id,
                    rules=(outcome.granted_rule,),
                ).decide(prepared_call, definition)
                if verification is not PermissionDecision.ALLOW:
                    return self._error(
                        call,
                        ToolErrorCode.PERMISSION_DENIED,
                        "Approval returned an invalid Session rule",
                        started,
                    )
                if self._recorder is not None:
                    await self._recorder.record(
                        context.turn_id,
                        RuntimeEventType.PERMISSION_RULE_GRANTED,
                        PermissionRuleGrantedPayload(outcome.granted_rule),
                    )
            await self._record_permission(context, call, decision, outcome.decision)
            if outcome.decision is ApprovalDecision.REJECT:
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
                ToolExecutionStartedPayload(call.call_id, call.name, definition.risk),
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
