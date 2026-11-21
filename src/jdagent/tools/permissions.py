"""在工具处理函数执行前评估权限规则。"""

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
    """在工具已经通过参数校验之后决定直接放行、询问用户或拒绝。"""

    def decide(self, call: ToolCall, definition: ToolDefinition) -> PermissionDecision: ...


class DefaultPermissionPolicy:
    """为本地工具提供默认决策，按风险决定是否需要询问用户。"""

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
    """在宿主配置的权限上限内应用当前会话的有效许可。"""

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
        """结合宿主设置、操作类型和会话规则判定权限；已有会话许可不能放宽明确拒绝的操作。"""
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
        """在授权事件成功保存后更新本轮使用的许可视图。"""
        self._rules = (*self._rules, rule)


def active_session_rules(events: tuple[RuntimeEvent, ...]) -> tuple[SessionPermissionRule, ...]:
    """按日志顺序应用授权和撤销事件，重建当前仍生效的会话规则。"""

    active: dict[str, SessionPermissionRule] = {}
    for event in events:
        if isinstance(event.payload, PermissionRuleGrantedPayload):
            active[event.payload.rule.rule_id] = event.payload.rule
        elif isinstance(event.payload, PermissionRuleRevokedPayload):
            active.pop(event.payload.rule_id, None)
    return tuple(active.values())


class StaticPermissionPolicy:
    """返回预先指定的权限决定，用于需要固定审批结果的测试。"""

    def __init__(self, decision: PermissionDecision) -> None:
        self._decision = decision

    @classmethod
    def deny(cls) -> "StaticPermissionPolicy":
        return cls(PermissionDecision.DENY)

    def decide(self, call: ToolCall, definition: ToolDefinition) -> PermissionDecision:
        del call, definition
        return self._decision
