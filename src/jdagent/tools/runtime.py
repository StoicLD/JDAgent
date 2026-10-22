"""Deep tool execution module: validation, permission, timeout, and normalization."""

import asyncio
import hashlib
import json
import time
from collections.abc import Iterable
from dataclasses import replace
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
    PermissionTargetKind,
    RiskLevel,
    ToolCall,
    ToolCapability,
    ToolDefinition,
    ToolErrorCode,
    ToolExecutionContext,
    ToolOutcome,
    ToolResult,
    ToolResultStatus,
)
from jdagent.instructions import TurnInstructions
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

    def definitions(
        self, capability: ToolCapability = ToolCapability.EXECUTE
    ) -> tuple[ToolDefinition, ...]:
        return tuple(tool for tool in self._definitions.values() if capability.allows(tool.risk))


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
        capability: ToolCapability = ToolCapability.EXECUTE,
        instructions: TurnInstructions | None = None,
    ) -> None:
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        self._registry = registry
        self._policy = policy
        self._approval = approval
        self._timeout_seconds = timeout_seconds
        self._recorder = recorder
        self._capability = capability
        self._instructions = instructions
        self._executed: set[tuple[str, str]] = set()
        self._rejected: set[tuple[str, str, str]] = set()

    async def execute(self, call: ToolCall, context: ToolExecutionContext) -> ToolResult:
        started = time.perf_counter()
        identity = (context.session_id, call.call_id)
        if identity in self._executed:
            return self._error(call, ToolErrorCode.NOT_EXECUTED, "Duplicate call ID", started)
        definition = self._registry.get(call.name)
        if definition is None:
            return self._error(call, ToolErrorCode.UNKNOWN_TOOL, "Unknown tool", started)
        if not self._capability.allows(definition.risk):
            return self._error(call, ToolErrorCode.TOOL_UNAVAILABLE, "Tool unavailable", started)

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
        if self._instructions is not None:
            try:
                await self._instructions.discover_arguments(prepared_arguments)
            except ValueError as error:
                return self._error(call, ToolErrorCode.CONFLICT, str(error), started)
            if (
                definition.risk in {RiskLevel.WRITE, RiskLevel.EXECUTE}
                and self._instructions.needs_replan
            ):
                return self._error(
                    call,
                    ToolErrorCode.INSTRUCTIONS_DISCOVERED,
                    "New directory instructions must be read before replanning this batch",
                    started,
                )
        prepared_snapshot = json.dumps(prepared_arguments, sort_keys=True, ensure_ascii=False)
        approval_identity = (context.turn_id, call.name, prepared_snapshot)
        if approval_identity in self._rejected:
            return self._error(
                call,
                ToolErrorCode.APPROVAL_REJECTED,
                "Identical invocation already rejected this turn",
                started,
            )
        decision = self._policy.decide(prepared_call, definition)
        if decision is PermissionDecision.DENY:
            await self._record_permission(context, call, decision)
            return self._error(
                call,
                ToolErrorCode.PERMISSION_DENIED,
                "Permission policy denied the tool call",
                started,
            )
        if (
            decision is PermissionDecision.ASK
            and definition.permission_target is PermissionTargetKind.MEMORY
            and context.memory_intent is not None
        ):
            decision = PermissionDecision.ALLOW
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
                dict(prepared_arguments),
            )
            request.arguments["display_sha256"] = hashlib.sha256(
                json.dumps(request.display, sort_keys=True, ensure_ascii=False).encode("utf-8")
            ).hexdigest()
            if self._recorder is not None:
                await self._recorder.record(
                    context.turn_id,
                    RuntimeEventType.PERMISSION_REQUESTED,
                    PermissionRequestedPayload(replace(request, display={})),
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
                if isinstance(self._policy, SessionPermissionPolicy):
                    self._policy.grant(outcome.granted_rule)
            await self._record_permission(context, call, decision, outcome.decision)
            if outcome.decision is ApprovalDecision.REJECT:
                self._rejected.add(approval_identity)
                return self._error(
                    call,
                    ToolErrorCode.APPROVAL_REJECTED,
                    "User rejected the tool call",
                    started,
                )

        else:
            await self._record_permission(context, call, decision)

        if json.dumps(prepared_arguments, sort_keys=True, ensure_ascii=False) != prepared_snapshot:
            return self._error(
                call, ToolErrorCode.CONFLICT, "Prepared approval content changed", started
            )
        self._executed.add(identity)
        if self._recorder is not None:
            await self._recorder.record(
                context.turn_id,
                RuntimeEventType.TOOL_EXECUTION_STARTED,
                ToolExecutionStartedPayload(call.call_id, call.name, definition.risk),
            )
        side_effect = definition.risk in (RiskLevel.WRITE, RiskLevel.EXECUTE)
        task = asyncio.ensure_future(
            definition.handler(prepared_arguments, replace(context, call_id=call.call_id))
        )
        try:
            output = await asyncio.wait_for(
                asyncio.shield(task),
                timeout=None if definition.manages_timeout else self._timeout_seconds,
            )
        except TimeoutError:
            task.cancel()
            try:
                await self._settle(task)
            except (asyncio.CancelledError, Exception):
                pass
            output = ToolOutcome(
                completed=False,
                effect="unknown" if side_effect else "none",
                error_code=ToolErrorCode.TIMEOUT,
                message="Tool execution timed out",
            )
        except asyncio.CancelledError:
            task.cancel()
            try:
                output = await self._settle(task)
            except (asyncio.CancelledError, Exception):
                output = ToolOutcome(completed=False, effect="unknown" if side_effect else "none")
            if not isinstance(output, ToolOutcome):
                output = ToolOutcome(data={"output": output})
            output = replace(output, cancelled=True, error_code=ToolErrorCode.CANCELLED)
        except Exception as error:
            output = ToolOutcome(
                completed=False,
                effect="unknown" if side_effect else "none",
                error_code=ToolErrorCode.EXECUTION_FAILED,
                message=f"{type(error).__name__}: tool handler failed",
            )
        if isinstance(output, ToolOutcome):
            if definition.result_schema is not None and output.error_code is None:
                try:
                    Draft202012Validator(definition.result_schema).validate(output.data)  # pyright: ignore[reportUnknownMemberType]
                except ValidationError:
                    output = replace(
                        output,
                        error_code=ToolErrorCode.EXECUTION_FAILED,
                        message="Invalid handler result",
                        requires_stop=side_effect,
                    )
            return ToolResult(
                call.call_id,
                call.name,
                ToolResultStatus.ERROR if output.error_code else ToolResultStatus.SUCCESS,
                output=json.dumps(output.to_data(), ensure_ascii=False),
                error_code=output.error_code,
                error_message=output.message or None,
                duration_ms=self._duration_ms(started),
                outcome=output,
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

    @staticmethod
    async def _settle(task: asyncio.Future[str | ToolOutcome]) -> str | ToolOutcome:
        """Retain task ownership even when the caller repeatedly cancels cleanup."""
        while True:
            try:
                return await asyncio.shield(task)
            except asyncio.CancelledError:
                if task.done():
                    raise

    @classmethod
    def _error(
        cls,
        call: ToolCall,
        code: ToolErrorCode,
        message: str,
        started: float,
    ) -> ToolResult:
        outcome = ToolOutcome(completed=False, error_code=code, message=message)
        return ToolResult(
            call.call_id,
            call.name,
            ToolResultStatus.ERROR,
            output=json.dumps(outcome.to_data(), ensure_ascii=False),
            error_code=code,
            error_message=message,
            duration_ms=cls._duration_ms(started),
            outcome=outcome,
        )

    @staticmethod
    def _duration_ms(started: float) -> int:
        return max(0, round((time.perf_counter() - started) * 1000))
