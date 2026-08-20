"""Translate terminal approval choices into validated domain outcomes."""

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
    """User-selectable lifetime and target scope."""

    ONCE = "once"
    SESSION_FILE = "session_file"
    SESSION_DIRECTORY = "session_directory"


@dataclass(frozen=True, slots=True)
class ApprovalChoice:
    """A terminal choice that does not itself contain a permission rule."""

    decision: ApprovalDecision
    scope: ApprovalScope = ApprovalScope.ONCE

    def __post_init__(self) -> None:
        if self.decision is ApprovalDecision.REJECT and self.scope is not ApprovalScope.ONCE:
            raise ValueError("A rejected choice cannot select a persistent scope")


class ApprovalChoicePort(Protocol):
    """Collect a user's visible choice without constructing domain rules."""

    async def request(self, request: ApprovalRequest) -> ApprovalChoice: ...


class ScopedApproval:
    """Build the narrow rule implied by a terminal choice and normalized request."""

    def __init__(self, choices: ApprovalChoicePort) -> None:
        self._choices = choices

    async def request(self, request: ApprovalRequest) -> ApprovalOutcome:
        choice = await self._choices.request(request)
        if choice.decision is ApprovalDecision.REJECT or choice.scope is ApprovalScope.ONCE:
            return ApprovalOutcome(choice.decision)
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
    """Reject side effects in non-interactive modes that cannot ask a user."""

    async def request(self, request: ApprovalRequest) -> ApprovalChoice:
        del request
        return ApprovalChoice(ApprovalDecision.REJECT)
