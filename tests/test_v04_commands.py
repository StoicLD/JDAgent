import asyncio
import ctypes
import os
import subprocess
import sys
import time
from ctypes import wintypes
from pathlib import Path

from jdagent.adapters.fake import FakeApproval
from jdagent.adapters.jsonl_session import JsonlSession
from jdagent.domain.events import RuntimeEventType, SessionStartedPayload
from jdagent.domain.tools import (
    ApprovalDecision,
    ApprovalOutcome,
    ApprovalRequest,
    PermissionDecision,
    PermissionTargetKind,
    SessionPermissionRule,
    ToolCall,
    ToolExecutionContext,
)
from jdagent.eventing import EventJournal
from jdagent.tools.command_output import CommandLogs
from jdagent.tools.commands import create_command_tools
from jdagent.tools.permissions import (
    SessionPermissionPolicy,
    StaticPermissionPolicy,
    active_session_rules,
)
from jdagent.tools.runtime import ToolRegistry, ToolRuntime


def test_invalid_executable_is_launch_failure_not_verification_failure(tmp_path: Path) -> None:
    executable = tmp_path / "invalid.exe"
    executable.write_bytes(b"This is not an executable")

    async def scenario() -> None:
        runtime = ToolRuntime(
            ToolRegistry(create_command_tools(tmp_path, CommandLogs(tmp_path / "logs"))),
            StaticPermissionPolicy(PermissionDecision.ALLOW),
            FakeApproval(ApprovalDecision.APPROVE),
        )
        result = await runtime.execute(
            ToolCall("c", "run_command", {"program": str(executable)}),
            ToolExecutionContext("s", "t", tmp_path),
        )
        assert result.outcome is not None
        assert result.outcome.completed is False
        assert result.outcome.observed_failure is False
        assert result.outcome.effect == "none"
        assert result.outcome.data["exit_code"] is None
        assert result.outcome.data["termination_reason"] == "launch_failed"
        assert not result.stop_turn

    asyncio.run(scenario())


def test_real_command_exit_and_persisted_output(tmp_path: Path) -> None:
    async def scenario() -> None:
        logs = CommandLogs(tmp_path / "logs")
        runtime = ToolRuntime(
            ToolRegistry(create_command_tools(tmp_path, logs)),
            StaticPermissionPolicy(PermissionDecision.ALLOW),
            FakeApproval(ApprovalDecision.APPROVE),
        )
        context = ToolExecutionContext("s", "t", tmp_path)
        result = await runtime.execute(
            ToolCall(
                "c",
                "run_command",
                {
                    "program": sys.executable,
                    "args": ["-c", "print('known output'); raise SystemExit(7)"],
                },
            ),
            context,
        )
        assert result.outcome is not None, result
        assert result.outcome.data["exit_code"] == 7, result
        assert result.outcome.observed_failure
        assert not result.stop_turn
        log_id = str(result.outcome.data["log_id"])
        reopened = CommandLogs(tmp_path / "logs")
        assert reopened.read("s", log_id, "stdout", 0, 100)["content"] == "known output\r\n"

    asyncio.run(scenario())


def test_killing_host_closes_job_and_terminates_child(tmp_path: Path) -> None:
    marker = tmp_path / "child.pid"
    child_code = (
        "import os,time,pathlib; "
        "pathlib.Path('child.pid').write_text(str(os.getpid())); time.sleep(60)"
    )
    host_code = (
        "import pathlib,sys,time; from jdagent.adapters.windows_process import WindowsCommand; "
        "from jdagent.tools.commands import command_environment; "
        f"job=WindowsCommand([sys.executable,'-c',{child_code!r}],"
        f"pathlib.Path({str(tmp_path)!r}),command_environment()); "
        "time.sleep(60)"
    )
    host = subprocess.Popen(
        [sys.executable, "-c", host_code],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    try:
        deadline = time.monotonic() + 10
        while not marker.exists() and host.poll() is None and time.monotonic() < deadline:
            time.sleep(0.02)
        assert marker.exists(), host.communicate(timeout=1)
        child_pid = int(marker.read_text())
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        kernel.WaitForSingleObject.restype = wintypes.DWORD
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = kernel.OpenProcess(0x100000, False, child_pid)
        assert handle
        try:
            assert kernel.WaitForSingleObject(handle, 0) == 258
            host.kill()
            host.wait(timeout=5)
            assert kernel.WaitForSingleObject(handle, 5000) == 0
        finally:
            kernel.CloseHandle(handle)
    finally:
        if host.poll() is None:
            host.kill()
        host.communicate(timeout=5)


def test_large_output_has_separate_preview_and_storage_loss(tmp_path: Path) -> None:
    async def scenario() -> None:
        logs = CommandLogs(tmp_path / "logs", call_limit=20000)
        runtime = ToolRuntime(
            ToolRegistry(create_command_tools(tmp_path, logs)),
            StaticPermissionPolicy(PermissionDecision.ALLOW),
            FakeApproval(ApprovalDecision.APPROVE),
        )
        result = await runtime.execute(
            ToolCall(
                "c",
                "run_command",
                {
                    "program": sys.executable,
                    "args": [
                        "-c",
                        "import sys; sys.stdout.buffer.write(b'x'*100000); "
                        "sys.stderr.buffer.write(b'e'*100000)",
                    ],
                },
            ),
            ToolExecutionContext("s", "t", tmp_path),
        )
        assert result.outcome is not None and not result.stop_turn
        assert result.outcome.data["log_loss"] is True
        assert result.outcome.data["preview_truncated"] is True
        log_id = str(result.outcome.data["log_id"])
        assert (
            sum(
                path.stat().st_size
                for path in (tmp_path / "logs" / "s" / log_id).iterdir()
                if path.name in {"stdout", "stderr"}
            )
            == 20000
        )
        logs.clear("s")
        assert logs.read("s", log_id, "stdout", 0, 100)["state"] == "cleared"

    asyncio.run(scenario())


def test_command_environment_does_not_inherit_provider_key(
    tmp_path: Path, monkeypatch: object
) -> None:
    from jdagent.tools.commands import command_environment

    previous = os.environ.get("DEEPSEEK_API_KEY")
    os.environ["DEEPSEEK_API_KEY"] = "test-secret"
    try:
        assert "DEEPSEEK_API_KEY" not in command_environment()
    finally:
        if previous is None:
            del os.environ["DEEPSEEK_API_KEY"]
        else:
            os.environ["DEEPSEEK_API_KEY"] = previous


def test_session_command_prefix_survives_reopen_and_deny_wins(tmp_path: Path) -> None:
    class GrantPrefix:
        def __init__(self) -> None:
            self.requests = 0

        async def request(self, request: ApprovalRequest) -> ApprovalOutcome:
            self.requests += 1
            return ApprovalOutcome(
                ApprovalDecision.APPROVE,
                SessionPermissionRule(
                    "rule",
                    request.session_id,
                    request.tool_name,
                    PermissionTargetKind.COMMAND,
                    str(request.display["program"]),
                    ("-c", "print('ok')"),
                    str(request.display["cwd"]),
                    str(request.display["environment_id"]),
                ),
            )

    async def scenario() -> None:
        session = JsonlSession(tmp_path / "sessions")
        journal = await EventJournal.open(session, "s")
        await journal.record(None, RuntimeEventType.SESSION_STARTED, SessionStartedPayload())
        registry = ToolRegistry(create_command_tools(tmp_path, CommandLogs(tmp_path / "logs")))
        approval = GrantPrefix()
        policy = SessionPermissionPolicy(workspace=tmp_path, session_id="s", rules=())
        runtime = ToolRuntime(registry, policy, approval, recorder=journal)
        args = {"program": sys.executable, "args": ["-c", "print('ok')"]}
        from jdagent.domain.json import normalize_json, require_object

        arguments = require_object(normalize_json(args), "arguments")
        result = await runtime.execute(
            ToolCall("c1", "run_command", arguments), ToolExecutionContext("s", "t", tmp_path)
        )
        assert result.error_code is None, result
        events = tuple([event async for event in JsonlSession(tmp_path / "sessions").read("s")])
        rules = active_session_rules(events)
        assert len(rules) == 1
        reopened = ToolRuntime(
            registry,
            SessionPermissionPolicy(workspace=tmp_path, session_id="s", rules=rules),
            approval,
        )
        arguments["args"] = ["-c", "print('ok')", "--variable-option"]
        result = await reopened.execute(
            ToolCall("c2", "run_command", arguments), ToolExecutionContext("s", "t2", tmp_path)
        )
        assert result.error_code is None, result
        assert approval.requests == 1
        denied = ToolRuntime(
            registry,
            SessionPermissionPolicy(
                workspace=tmp_path,
                session_id="s",
                rules=rules,
                command_ceiling=PermissionDecision.DENY,
            ),
            approval,
        )
        result = await denied.execute(
            ToolCall("c3", "run_command", arguments), ToolExecutionContext("s", "t3", tmp_path)
        )
        assert result.error_code is not None and result.error_code.value == "permission_denied"
        assert approval.requests == 1

    asyncio.run(scenario())


def test_timeout_terminates_real_process_tree_and_stops(tmp_path: Path) -> None:
    async def scenario() -> None:
        runtime = ToolRuntime(
            ToolRegistry(create_command_tools(tmp_path, CommandLogs(tmp_path / "logs"))),
            StaticPermissionPolicy(PermissionDecision.ALLOW),
            FakeApproval(ApprovalDecision.APPROVE),
        )
        result = await runtime.execute(
            ToolCall(
                "c",
                "run_command",
                {
                    "program": sys.executable,
                    "args": ["-c", "import time; time.sleep(30)"],
                    "timeout_seconds": 0.2,
                },
            ),
            ToolExecutionContext("s", "t", tmp_path),
        )
        assert result.stop_turn
        assert result.outcome is not None
        assert result.outcome.data["cleanup_confirmed"] is True, result
        assert result.outcome.data["termination_reason"] == "timeout"

    asyncio.run(scenario())
