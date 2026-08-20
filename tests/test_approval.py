import asyncio
from pathlib import Path

from jdagent.adapters.terminal import format_approval_request
from jdagent.application.approval import ApprovalChoice, ApprovalScope, ScopedApproval
from jdagent.domain.tools import (
    ApprovalDecision,
    ApprovalRequest,
    PermissionTargetKind,
    RiskLevel,
)


def test_scoped_approval_builds_rule_from_normalized_request_target(tmp_path: Path) -> None:
    class ChoicePort:
        async def request(self, request: ApprovalRequest) -> ApprovalChoice:
            assert request.target == "notes/approved.txt"
            return ApprovalChoice(ApprovalDecision.APPROVE, ApprovalScope.SESSION_FILE)

    request = ApprovalRequest(
        tool_name="write_text_file",
        arguments={"path": str(tmp_path / "notes" / "approved.txt"), "content": "secret"},
        risk=RiskLevel.WRITE,
        call_id="call-1",
        session_id="session-1",
        target="notes/approved.txt",
    )

    outcome = asyncio.run(ScopedApproval(ChoicePort()).request(request))

    assert outcome.decision is ApprovalDecision.APPROVE
    assert outcome.granted_rule is not None
    assert outcome.granted_rule.session_id == "session-1"
    assert outcome.granted_rule.target_kind is PermissionTargetKind.FILE
    assert outcome.granted_rule.target == "notes/approved.txt"
    assert "secret" not in repr(outcome.granted_rule)


def test_approval_view_shows_safe_summary_without_file_content() -> None:
    request = ApprovalRequest(
        tool_name="write_text_file",
        arguments={"content": "<18 chars>", "path": "private/note.txt"},
        risk=RiskLevel.WRITE,
        call_id="call-1",
        session_id="session-1",
        target="private/note.txt",
    )

    rendered = format_approval_request(request)

    assert "private/note.txt" in rendered
    assert "<18 chars>" in rendered
    assert "api-secret-content" not in rendered
