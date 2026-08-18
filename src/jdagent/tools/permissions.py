"""Pure permission policies evaluated before tool handlers."""

from typing import Protocol

from jdagent.domain.tools import PermissionDecision, RiskLevel, ToolCall, ToolDefinition


class PermissionPolicy(Protocol):
    """Decide whether a validated tool call may proceed or needs approval."""

    def decide(self, call: ToolCall, definition: ToolDefinition) -> PermissionDecision: ...


class DefaultPermissionPolicy:
    """Allow pure/read tools and ask before writes."""

    def decide(self, call: ToolCall, definition: ToolDefinition) -> PermissionDecision:
        del call
        if definition.risk in (RiskLevel.PURE, RiskLevel.READ):
            return PermissionDecision.ALLOW
        if definition.risk is RiskLevel.WRITE:
            return PermissionDecision.ASK
        return PermissionDecision.DENY


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
