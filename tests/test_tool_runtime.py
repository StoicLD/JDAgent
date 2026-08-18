import asyncio
import os
from pathlib import Path

import pytest

from jdagent.adapters.fake import FakeApproval
from jdagent.domain.json import JsonObject
from jdagent.domain.tools import (
    ApprovalDecision,
    ApprovalRequest,
    RiskLevel,
    ToolCall,
    ToolDefinition,
    ToolErrorCode,
    ToolExecutionContext,
    ToolResultStatus,
)
from jdagent.tools.builtins import create_builtin_tools
from jdagent.tools.permissions import DefaultPermissionPolicy, StaticPermissionPolicy
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

        async def request(self, request: ApprovalRequest) -> ApprovalDecision:
            del request
            self.requested_before_write = not target.exists()
            return ApprovalDecision.APPROVE

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
