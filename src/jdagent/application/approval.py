"""将终端上的授权选择转换成经过范围校验的权限决定。"""

from dataclasses import dataclass
from enum import StrEnum
from pathlib import PurePosixPath
from typing import Protocol
from uuid import uuid4

from jdagent.domain.tools import (
    ApprovalDecision,
    ApprovalOutcome,
    ApprovalRequest,
    PermissionTargetKind,
    SessionPermissionRule,
)


class ApprovalScope(StrEnum):
    """表示用户此次授权覆盖的目标与有效期限。"""

    ONCE = "once"
    SESSION_FILE = "session_file"
    SESSION_DIRECTORY = "session_directory"
    SESSION_COMMAND = "session_command"


@dataclass(frozen=True, slots=True)
class ApprovalChoice:
    """记录用户在终端选中的审批选项，具体权限规则由应用层生成。"""

    decision: ApprovalDecision
    scope: ApprovalScope = ApprovalScope.ONCE
    prefix_length: int = 0

    def __post_init__(self) -> None:
        if self.decision is ApprovalDecision.REJECT and self.scope is not ApprovalScope.ONCE:
            raise ValueError("A rejected choice cannot select a persistent scope")


class ApprovalChoicePort(Protocol):
    """接收用户在界面上作出的选择。生成权限规则由审批服务负责。"""

    async def request(self, request: ApprovalRequest) -> ApprovalChoice: ...


class ScopedApproval:
    """根据用户选择及已规范化的操作目标生成相应范围的权限规则。"""

    def __init__(self, choices: ApprovalChoicePort) -> None:
        self._choices = choices

    async def request(self, request: ApprovalRequest) -> ApprovalOutcome:
        """针对当前操作展示审批请求，把可用的授权范围转换为本次决定及可选的会话规则。"""
        choice = await self._choices.request(request)
        if choice.decision is ApprovalDecision.REJECT or choice.scope is ApprovalScope.ONCE:
            return ApprovalOutcome(choice.decision)
        if choice.scope is ApprovalScope.SESSION_COMMAND:
            args = request.display.get("args")
            program = request.display.get("program")
            cwd = request.display.get("cwd")
            environment_id = request.display.get("environment_id")
            if (
                not isinstance(args, list)
                or not 1 <= choice.prefix_length <= len(args)
                or not isinstance(program, str)
                or not isinstance(cwd, str)
                or not isinstance(environment_id, str)
                or not request.session_id
            ):
                return ApprovalOutcome(ApprovalDecision.REJECT)
            return ApprovalOutcome(
                ApprovalDecision.APPROVE,
                SessionPermissionRule(
                    str(uuid4()),
                    request.session_id,
                    request.tool_name,
                    PermissionTargetKind.COMMAND,
                    program,
                    tuple(str(value) for value in args[: choice.prefix_length]),
                    cwd,
                    environment_id,
                ),
            )
        if not request.session_id or request.target is None:
            return ApprovalOutcome(ApprovalDecision.REJECT)
        target = PurePosixPath(request.target)
        if target.is_absolute() or ".." in target.parts:
            return ApprovalOutcome(ApprovalDecision.REJECT)
        if choice.scope is ApprovalScope.SESSION_DIRECTORY:
            target = target.parent
            target_kind = PermissionTargetKind.DIRECTORY
        else:
            target_kind = PermissionTargetKind.FILE
        return ApprovalOutcome(
            ApprovalDecision.APPROVE,
            SessionPermissionRule(
                rule_id=str(uuid4()),
                session_id=request.session_id,
                tool_name=request.tool_name,
                target_kind=target_kind,
                target=target.as_posix(),
            ),
        )


class RejectingApprovalChoices:
    """无法询问用户时拒绝需要批准的操作，供非交互入口使用。"""

    async def request(self, request: ApprovalRequest) -> ApprovalChoice:
        del request
        return ApprovalChoice(ApprovalDecision.REJECT)
