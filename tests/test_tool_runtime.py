import asyncio
import os
from pathlib import Path

import pytest

from jdagent.adapters.fake import FakeApproval
from jdagent.adapters.memory import InMemorySession
from jdagent.application.permissions import revoke_session_rule
from jdagent.domain.events import RuntimeEventType, SessionStartedPayload
from jdagent.domain.json import JsonObject
from jdagent.domain.tools import (
    ApprovalDecision,
    ApprovalOutcome,
    ApprovalRequest,
    PermissionDecision,
    PermissionTargetKind,
    RiskLevel,
    SessionPermissionRule,
    ToolCall,
    ToolDefinition,
    ToolErrorCode,
    ToolExecutionContext,
    ToolResultStatus,
)
from jdagent.eventing import EventJournal
from jdagent.tools.builtins import create_builtin_tools
from jdagent.tools.permissions import (
    DefaultPermissionPolicy,
    SessionPermissionPolicy,
    StaticPermissionPolicy,
    active_session_rules,
)
from jdagent.tools.runtime import ToolRegistry, ToolRuntime
from jdagent.tools.workspace import WorkspacePathError, WorkspacePathResolver


def _context(workspace: Path) -> ToolExecutionContext:
    return ToolExecutionContext("session-1", "turn-1", workspace)


def test_tool_runtime_rejects_invalid_arguments_before_handler(tmp_path: Path) -> None:
    executed = False

    async def handler(arguments: JsonObject, context: ToolExecutionContext) -> str:
        nonlocal executed
        del arguments, context
        executed = True
        return "unexpected"

    tool = ToolDefinition(
        name="echo",
        description="Echo text.",
        input_schema={
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
            "additionalProperties": False,
        },
        risk=RiskLevel.PURE,
        handler=handler,
    )
    runtime = ToolRuntime(
        ToolRegistry((tool,)),
        DefaultPermissionPolicy(),
        FakeApproval(ApprovalDecision.APPROVE),
    )

    result = asyncio.run(runtime.execute(ToolCall("call-1", "echo", {}), _context(tmp_path)))

    assert result.status is ToolResultStatus.ERROR
    assert result.error_code is ToolErrorCode.INVALID_ARGUMENTS
    assert executed is False


def test_workspace_resolver_rejects_parent_escape(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    resolver = WorkspacePathResolver(workspace)

    with pytest.raises(WorkspacePathError):
        resolver.resolve_write("../outside.txt")


def test_workspace_resolver_rejects_symlink_escape(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    outside = tmp_path / "outside"
    workspace.mkdir()
    outside.mkdir()
    link = workspace / "escape"
    try:
        os.symlink(outside, link, target_is_directory=True)
    except OSError as error:
        pytest.skip(f"Symlink creation is unavailable: {error}")
    resolver = WorkspacePathResolver(workspace)

    with pytest.raises(WorkspacePathError):
        resolver.resolve_write("escape/result.txt")


def test_file_tools_reject_outside_workspace_before_handler(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    outside = tmp_path / "outside"
    workspace.mkdir()
    outside.mkdir()
    outside_read = outside / "private.txt"
    outside_read.write_text("private", encoding="utf-8")
    outside_write = outside / "created.txt"
    runtime = ToolRuntime(
        ToolRegistry(create_builtin_tools(WorkspacePathResolver(workspace))),
        DefaultPermissionPolicy(),
        FakeApproval(ApprovalDecision.APPROVE),
    )

    read_result = asyncio.run(
        runtime.execute(
            ToolCall("read-1", "read_text_file", {"path": str(outside_read)}),
            _context(workspace),
        )
    )
    write_result = asyncio.run(
        runtime.execute(
            ToolCall(
                "write-1",
                "write_text_file",
                {"path": str(outside_write), "content": "unexpected"},
            ),
            _context(workspace),
        )
    )

    assert read_result.error_code is ToolErrorCode.PATH_OUTSIDE_WORKSPACE
    assert write_result.error_code is ToolErrorCode.PATH_OUTSIDE_WORKSPACE
    assert outside_read.read_text(encoding="utf-8") == "private"
    assert not outside_write.exists()


def test_write_tool_does_not_run_before_approval(tmp_path: Path) -> None:
    target = tmp_path / "note.txt"

    class ObservingApproval:
        def __init__(self) -> None:
            self.requested_before_write = False

        async def request(self, request: ApprovalRequest) -> ApprovalOutcome:
            del request
            self.requested_before_write = not target.exists()
            return ApprovalOutcome(ApprovalDecision.APPROVE)

    registry = ToolRegistry(create_builtin_tools(WorkspacePathResolver(tmp_path)))
    approval = ObservingApproval()
    runtime = ToolRuntime(registry, DefaultPermissionPolicy(), approval)

    result = asyncio.run(
        runtime.execute(
            ToolCall(
                "call-1",
                "write_text_file",
                {"path": "note.txt", "content": "secret"},
            ),
            _context(tmp_path),
        )
    )

    assert approval.requested_before_write is True
    assert result.status is ToolResultStatus.SUCCESS
    assert target.read_text(encoding="utf-8") == "secret"


def test_approval_rejection_returns_tool_result(tmp_path: Path) -> None:
    registry = ToolRegistry(create_builtin_tools(WorkspacePathResolver(tmp_path)))
    approval = FakeApproval(ApprovalDecision.REJECT)
    runtime = ToolRuntime(registry, DefaultPermissionPolicy(), approval)
    target = tmp_path / "note.txt"

    result = asyncio.run(
        runtime.execute(
            ToolCall(
                "call-1",
                "write_text_file",
                {"path": "note.txt", "content": "secret"},
            ),
            _context(tmp_path),
        )
    )

    assert result.status is ToolResultStatus.ERROR
    assert result.error_code is ToolErrorCode.APPROVAL_REJECTED
    assert not target.exists()
    assert len(approval.requests) == 1


def test_denied_tool_never_calls_handler(tmp_path: Path) -> None:
    executed = False

    async def handler(arguments: JsonObject, context: ToolExecutionContext) -> str:
        nonlocal executed
        del arguments, context
        executed = True
        return "unexpected"

    tool = ToolDefinition(
        "dangerous",
        "A denied tool.",
        {"type": "object", "additionalProperties": False},
        RiskLevel.WRITE,
        handler,
    )
    runtime = ToolRuntime(
        ToolRegistry((tool,)),
        StaticPermissionPolicy.deny(),
        FakeApproval(ApprovalDecision.APPROVE),
    )

    result = asyncio.run(runtime.execute(ToolCall("call-1", "dangerous", {}), _context(tmp_path)))

    assert result.error_code is ToolErrorCode.PERMISSION_DENIED
    assert executed is False


def test_unknown_tool_returns_stable_error(tmp_path: Path) -> None:
    runtime = ToolRuntime(
        ToolRegistry(()),
        DefaultPermissionPolicy(),
        FakeApproval(ApprovalDecision.APPROVE),
    )

    result = asyncio.run(runtime.execute(ToolCall("call-1", "missing", {}), _context(tmp_path)))

    assert result.error_code is ToolErrorCode.UNKNOWN_TOOL


def test_handler_exception_returns_stable_error(tmp_path: Path) -> None:
    async def broken_handler(arguments: JsonObject, context: ToolExecutionContext) -> str:
        del arguments, context
        raise RuntimeError("private failure detail")

    tool = ToolDefinition(
        "broken",
        "Always fails.",
        {"type": "object", "additionalProperties": False},
        RiskLevel.PURE,
        broken_handler,
    )
    runtime = ToolRuntime(
        ToolRegistry((tool,)),
        DefaultPermissionPolicy(),
        FakeApproval(ApprovalDecision.APPROVE),
    )

    result = asyncio.run(runtime.execute(ToolCall("call-1", "broken", {}), _context(tmp_path)))

    assert result.error_code is ToolErrorCode.EXECUTION_FAILED
    assert "private failure detail" not in (result.error_message or "")


def test_session_rule_matches_only_normalized_approved_target(tmp_path: Path) -> None:
    rule = SessionPermissionRule(
        rule_id="rule-1",
        session_id="session-1",
        tool_name="write_text_file",
        target_kind=PermissionTargetKind.FILE,
        target="notes/approved.txt",
    )
    policy = SessionPermissionPolicy(
        workspace=tmp_path,
        session_id="session-1",
        rules=(rule,),
        write_ceiling=PermissionDecision.ASK,
    )
    definition = create_builtin_tools(WorkspacePathResolver(tmp_path))[2]

    approved = policy.decide(
        ToolCall(
            "approved",
            "write_text_file",
            {"path": str(tmp_path / "notes" / "approved.txt"), "content": "ok"},
        ),
        definition,
    )
    sibling = policy.decide(
        ToolCall(
            "sibling",
            "write_text_file",
            {"path": str(tmp_path / "notes" / "other.txt"), "content": "no"},
        ),
        definition,
    )

    assert approved is PermissionDecision.ALLOW
    assert sibling is PermissionDecision.ASK


def test_directory_rule_is_contained_and_does_not_leak_or_override_deny(
    tmp_path: Path,
) -> None:
    rule = SessionPermissionRule(
        "rule-1",
        "session-1",
        "write_text_file",
        PermissionTargetKind.DIRECTORY,
        "notes",
    )
    definition = create_builtin_tools(WorkspacePathResolver(tmp_path))[2]
    child = ToolCall(
        "call-1",
        "write_text_file",
        {"path": "notes/nested/file.txt", "content": "ok"},
    )

    allowed = SessionPermissionPolicy(
        workspace=tmp_path,
        session_id="session-1",
        rules=(rule,),
    ).decide(child, definition)
    another_session = SessionPermissionPolicy(
        workspace=tmp_path,
        session_id="session-2",
        rules=(rule,),
    ).decide(child, definition)
    denied_ceiling = SessionPermissionPolicy(
        workspace=tmp_path,
        session_id="session-1",
        rules=(rule,),
        write_ceiling=PermissionDecision.DENY,
    ).decide(child, definition)

    assert allowed is PermissionDecision.ALLOW
    assert another_session is PermissionDecision.ASK
    assert denied_ceiling is PermissionDecision.DENY


def test_approval_request_redacts_write_content_and_uses_relative_target(
    tmp_path: Path,
) -> None:
    approval = FakeApproval(ApprovalDecision.REJECT)
    runtime = ToolRuntime(
        ToolRegistry(create_builtin_tools(WorkspacePathResolver(tmp_path))),
        SessionPermissionPolicy(
            workspace=tmp_path,
            session_id="session-1",
            rules=(),
        ),
        approval,
    )

    result = asyncio.run(
        runtime.execute(
            ToolCall(
                "call-1",
                "write_text_file",
                {"path": "private/note.txt", "content": "api-secret-content"},
            ),
            _context(tmp_path),
        )
    )

    assert result.error_code is ToolErrorCode.APPROVAL_REJECTED
    assert approval.requests[0].target == "private/note.txt"
    assert approval.requests[0].arguments == {
        "content": "<18 chars>",
        "path": "private/note.txt",
    }
    assert "api-secret-content" not in repr(approval.requests[0])


def test_approved_session_rule_is_persisted_before_write(tmp_path: Path) -> None:
    async def scenario() -> tuple[ToolResultStatus, tuple[RuntimeEventType, ...]]:
        session = InMemorySession()
        journal = await EventJournal.open(session, "session-1")
        await journal.record(None, RuntimeEventType.SESSION_STARTED, SessionStartedPayload())
        rule = SessionPermissionRule(
            rule_id="rule-1",
            session_id="session-1",
            tool_name="write_text_file",
            target_kind=PermissionTargetKind.FILE,
            target="note.txt",
        )
        runtime = ToolRuntime(
            ToolRegistry(create_builtin_tools(WorkspacePathResolver(tmp_path))),
            SessionPermissionPolicy(
                workspace=tmp_path,
                session_id="session-1",
                rules=(),
            ),
            FakeApproval(ApprovalOutcome(ApprovalDecision.APPROVE, rule)),
            recorder=journal,
        )
        result = await runtime.execute(
            ToolCall(
                "call-1",
                "write_text_file",
                {"path": "note.txt", "content": "persisted"},
            ),
            _context(tmp_path),
        )
        return result.status, tuple(event.event_type for event in journal.events)

    status, event_types = asyncio.run(scenario())

    assert status is ToolResultStatus.SUCCESS
    assert RuntimeEventType.PERMISSION_RULE_GRANTED in event_types
    assert (tmp_path / "note.txt").read_text(encoding="utf-8") == "persisted"


def test_session_rule_survives_reopen_until_revoked(tmp_path: Path) -> None:
    async def scenario() -> tuple[int, int]:
        session = InMemorySession()
        journal = await EventJournal.open(session, "session-1")
        await journal.record(None, RuntimeEventType.SESSION_STARTED, SessionStartedPayload())
        rule = SessionPermissionRule(
            "rule-1",
            "session-1",
            "write_text_file",
            PermissionTargetKind.FILE,
            "note.txt",
        )
        granting = ToolRuntime(
            ToolRegistry(create_builtin_tools(WorkspacePathResolver(tmp_path))),
            SessionPermissionPolicy(
                workspace=tmp_path,
                session_id="session-1",
                rules=(),
            ),
            FakeApproval(ApprovalOutcome(ApprovalDecision.APPROVE, rule)),
            recorder=journal,
        )
        await granting.execute(
            ToolCall("call-1", "write_text_file", {"path": "note.txt", "content": "first"}),
            _context(tmp_path),
        )

        reopened = await EventJournal.open(session, "session-1", require_existing=True)
        rejecting_approval = FakeApproval(ApprovalDecision.REJECT)
        resumed = ToolRuntime(
            ToolRegistry(create_builtin_tools(WorkspacePathResolver(tmp_path))),
            SessionPermissionPolicy(
                workspace=tmp_path,
                session_id="session-1",
                rules=active_session_rules(reopened.events),
            ),
            rejecting_approval,
            recorder=reopened,
        )
        await resumed.execute(
            ToolCall("call-2", "write_text_file", {"path": "note.txt", "content": "second"}),
            _context(tmp_path),
        )
        requests_before_revoke = len(rejecting_approval.requests)
        await revoke_session_rule(session, "session-1", "rule-1")
        after_revoke = await EventJournal.open(session, "session-1", require_existing=True)
        remaining = len(active_session_rules(after_revoke.events))
        return requests_before_revoke, remaining

    approval_requests, remaining_rules = asyncio.run(scenario())

    assert approval_requests == 0
    assert remaining_rules == 0
    assert (tmp_path / "note.txt").read_text(encoding="utf-8") == "second"
