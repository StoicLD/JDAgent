"""Pure permission policies evaluated before tool handlers."""

from pathlib import Path, PurePosixPath
from typing import Protocol

from jdagent.domain.events import (
    PermissionRuleGrantedPayload,
    PermissionRuleRevokedPayload,
    RuntimeEvent,
)
from jdagent.domain.tools import (
    PermissionDecision,
    PermissionTargetKind,
    RiskLevel,
    SessionPermissionRule,
    ToolCall,
    ToolDefinition,
)
from jdagent.tools.workspace import WorkspacePathError, WorkspacePathResolver


class PermissionPolicy(Protocol):
    """Decide whether a validated tool call may proceed or needs approval."""

    def decide(self, call: ToolCall, definition: ToolDefinition) -> PermissionDecision: ...


class DefaultPermissionPolicy:
    """Allow pure/read tools and ask before writes."""

    def __init__(
        self,
        write_ceiling: PermissionDecision = PermissionDecision.ASK,
        command_ceiling: PermissionDecision = PermissionDecision.ASK,
    ) -> None:
        if write_ceiling not in {PermissionDecision.ASK, PermissionDecision.DENY}:
            raise ValueError("write_ceiling must be ask or deny")
        self._write_ceiling = write_ceiling
        self._command_ceiling = command_ceiling

    def decide(self, call: ToolCall, definition: ToolDefinition) -> PermissionDecision:
        del call
        if definition.risk in (RiskLevel.PURE, RiskLevel.READ):
            return PermissionDecision.ALLOW
        if definition.risk is RiskLevel.WRITE:
            return self._write_ceiling
        if definition.risk is RiskLevel.EXECUTE:
            return (
                PermissionDecision.DENY
                if self._write_ceiling is PermissionDecision.DENY
                else self._command_ceiling
            )
        return PermissionDecision.DENY


class SessionPermissionPolicy:
    """Apply active Session rules below a non-bypassable write ceiling."""

    def __init__(
        self,
        *,
        workspace: Path,
        session_id: str,
        rules: tuple[SessionPermissionRule, ...],
        write_ceiling: PermissionDecision = PermissionDecision.ASK,
        command_ceiling: PermissionDecision = PermissionDecision.ASK,
    ) -> None:
        self._base = DefaultPermissionPolicy(write_ceiling, command_ceiling)
        self._resolver = WorkspacePathResolver(workspace)
        self._workspace = workspace.resolve(strict=True)
        self._session_id = session_id
        self._rules = rules

    def decide(self, call: ToolCall, definition: ToolDefinition) -> PermissionDecision:
        decision = self._base.decide(call, definition)
        if decision is not PermissionDecision.ASK:
            return decision
        if definition.risk is RiskLevel.EXECUTE:
            argv = call.arguments.get("args")
            if not isinstance(argv, list):
                return PermissionDecision.DENY
            for rule in self._rules:
                if (
                    rule.target_kind is PermissionTargetKind.COMMAND
                    and rule.session_id == self._session_id
                    and rule.tool_name == call.name
                    and rule.target == call.arguments.get("program")
                    and rule.cwd == call.arguments.get("cwd")
                    and rule.environment_id == call.arguments.get("environment_id")
                    and rule.args_prefix
                    and list(rule.args_prefix) == argv[: len(rule.args_prefix)]
                ):
                    return PermissionDecision.ALLOW
            return PermissionDecision.ASK
        raw_path = call.arguments.get("path")
        if not isinstance(raw_path, str):
            return PermissionDecision.ASK
        try:
            resolved = self._resolver.resolve_write(raw_path)
            relative = PurePosixPath(resolved.relative_to(self._workspace).as_posix())
        except (OSError, ValueError, WorkspacePathError):
            return PermissionDecision.DENY
        for rule in self._rules:
            if rule.session_id != self._session_id or rule.tool_name != call.name:
                continue
            target = PurePosixPath(rule.target)
            if rule.target_kind is PermissionTargetKind.FILE and relative == target:
                return PermissionDecision.ALLOW
            if rule.target_kind is PermissionTargetKind.DIRECTORY and (
                relative == target or target in relative.parents
            ):
                return PermissionDecision.ALLOW
        return PermissionDecision.ASK

    def grant(self, rule: SessionPermissionRule) -> None:
        """Refresh the turn's rule projection after the grant is durably saved."""
        self._rules = (*self._rules, rule)


def active_session_rules(events: tuple[RuntimeEvent, ...]) -> tuple[SessionPermissionRule, ...]:
    """Project active rules from durable grant/revoke facts."""

    active: dict[str, SessionPermissionRule] = {}
    for event in events:
        if isinstance(event.payload, PermissionRuleGrantedPayload):
            active[event.payload.rule.rule_id] = event.payload.rule
        elif isinstance(event.payload, PermissionRuleRevokedPayload):
            active.pop(event.payload.rule_id, None)
    return tuple(active.values())


class StaticPermissionPolicy:
    """A deterministic policy adapter for tests."""

    def __init__(self, decision: PermissionDecision) -> None:
        self._decision = decision

    @classmethod
    def deny(cls) -> "StaticPermissionPolicy":
        return cls(PermissionDecision.DENY)

    def decide(self, call: ToolCall, definition: ToolDefinition) -> PermissionDecision:
        del call, definition
        return self._decision
