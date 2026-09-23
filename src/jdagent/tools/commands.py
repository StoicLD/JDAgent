"""Prepared command identity, Job lifecycle and session-scoped output access."""

import asyncio
import hashlib
import json
import os
import shutil
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path

from jdagent.adapters.windows_files import locked_file
from jdagent.adapters.windows_process import WindowsCommand
from jdagent.domain.json import JsonObject, require_array, require_integer, require_string
from jdagent.domain.tools import (
    RiskLevel,
    ToolDefinition,
    ToolErrorCode,
    ToolExecutionContext,
    ToolOutcome,
)
from jdagent.tools.builtins import object_schema
from jdagent.tools.command_output import CommandLogs
from jdagent.tools.files import checked_path

_ENVIRONMENT = {
    "SYSTEMROOT",
    "WINDIR",
    "PATH",
    "PATHEXT",
    "TEMP",
    "TMP",
    "COMSPEC",
    "USERPROFILE",
    "APPDATA",
    "LOCALAPPDATA",
    "PROGRAMFILES",
    "PROGRAMFILES(X86)",
    "PROGRAMDATA",
    "NUMBER_OF_PROCESSORS",
    "PROCESSOR_ARCHITECTURE",
}


def command_environment() -> dict[str, str]:
    """Copy a bounded host allowlist, excluding API credentials and Python hooks."""
    return {key: value for key, value in os.environ.items() if key.upper() in _ENVIRONMENT}


def create_command_tools(workspace: Path, logs: CommandLogs) -> tuple[ToolDefinition, ...]:
    root = workspace.resolve(strict=True)
    environment = command_environment()
    environment_id = hashlib.sha256(json.dumps(environment, sort_keys=True).encode()).hexdigest()
    text_schema: JsonObject = {"type": "string"}
    count_schema: JsonObject = {"type": "integer", "minimum": 0}
    stream_schema = object_schema(
        {
            "preview": text_schema,
            "total_bytes": count_schema,
            "saved_bytes": count_schema,
            "replacement_characters": {"type": "boolean"},
        },
        ["preview", "total_bytes", "saved_bytes", "replacement_characters"],
    )
    command_result = object_schema(
        {
            "log_id": text_schema,
            "streams": object_schema(
                {"stdout": stream_schema, "stderr": stream_schema}, ["stdout", "stderr"]
            ),
            "preview_truncated": {"type": "boolean"},
            "log_loss": {"type": "boolean"},
            "program": text_schema,
            "cwd": text_schema,
            "exit_code": {"type": "integer"},
            "process_started": {"const": True},
            "termination_reason": {"const": "exit"},
            "cleanup_confirmed": {"const": True},
            "pid": {"type": "integer", "minimum": 1},
        },
        [
            "log_id",
            "streams",
            "preview_truncated",
            "log_loss",
            "program",
            "cwd",
            "exit_code",
            "process_started",
            "termination_reason",
            "cleanup_confirmed",
            "pid",
        ],
    )
    output_result: JsonObject = {
        "oneOf": [
            object_schema(
                {"log_id": text_schema, "session_id": text_schema, "state": {"const": "cleared"}},
                ["log_id", "session_id", "state"],
            ),
            object_schema(
                {
                    "log_id": text_schema,
                    "session_id": text_schema,
                    "stream": {"enum": ["stdout", "stderr"]},
                    "offset": count_schema,
                    "content": text_schema,
                    "next_offset": count_schema,
                    "metadata": {"type": "object"},
                },
                ["log_id", "session_id", "stream", "offset", "content", "next_offset", "metadata"],
            ),
        ]
    }

    def prepare(args: JsonObject, context: ToolExecutionContext) -> JsonObject:
        del context
        program = require_string(args, "program")
        cwd = checked_path(root, str(args.get("cwd", ".")), directory=True)
        found = shutil.which(program, path=environment.get("PATH", environment.get("Path", "")))
        if found is None:
            raise ValueError("Program not found")
        executable = Path(found).resolve(strict=True)
        if executable.suffix.lower() not in {".exe", ".com"}:
            raise ValueError("Only explicit executable programs are supported; no bat/cmd")
        argv = require_array(args.get("args", []), "args")
        if any(not isinstance(value, str) or "\x00" in value for value in argv):
            raise ValueError("Arguments must be NUL-free strings")
        if len(json.dumps(argv).encode("utf-16-le")) > 48000:
            raise ValueError("Command arguments too large")
        return {
            **args,
            "program": str(executable),
            "args": argv,
            "cwd": str(cwd),
            "environment_id": environment_id,
        }

    def execute(
        args: JsonObject, context: ToolExecutionContext, cancellation: threading.Event
    ) -> ToolOutcome:
        actual_cwd = checked_path(root, require_string(args, "cwd"), directory=True)
        program = require_string(args, "program")
        argv = [program, *(str(value) for value in require_array(args.get("args"), "args"))]
        timeout = float(str(args.get("timeout_seconds", 120)))
        logs.root.mkdir(parents=True, exist_ok=True)
        lock_path = logs.root / "capacity.lock"
        lock_path.touch(exist_ok=True)
        try:
            lock = locked_file(lock_path, write=True)
            lock.__enter__()
        except OSError:
            return ToolOutcome(
                completed=False, error_code=ToolErrorCode.CAPACITY, message="Output storage busy"
            )
        process: WindowsCommand | None = None
        readers: list[Future[None]] = []
        reason = "exit"
        exit_code: int | None = None
        output: JsonObject = {}
        pool = ThreadPoolExecutor(max_workers=2)
        try:
            try:
                log = logs.start(context.session_id, context.call_id)
            except OSError:
                return ToolOutcome(
                    completed=False,
                    error_code=ToolErrorCode.CAPACITY,
                    message="Output capacity exhausted; clear logs",
                )
            output["log_id"] = log.log_id
            if cancellation.is_set():
                return ToolOutcome(completed=False, cancelled=True, data=output)
            process = WindowsCommand(argv, actual_cwd, environment, log.status_file)
            readers = [
                pool.submit(log.drain, "stdout", process.stdout),
                pool.submit(log.drain, "stderr", process.stderr),
            ]
            deadline = time.monotonic() + timeout
            while process.poll() is None:
                if cancellation.is_set():
                    reason = "cancelled"
                    break
                if any(reader.done() and reader.exception() is not None for reader in readers):
                    reason = "output_error"
                    break
                if time.monotonic() >= deadline:
                    reason = "timeout"
                    break
                cancellation.wait(0.02)
            exit_code = process.poll()
            if reason == "exit":
                for _ in range(10):
                    if process.active_processes() == 0:
                        break
                    time.sleep(0.02)
            if process.active_processes():
                if reason == "exit":
                    reason = "descendants_terminated"
                process.terminate()
            cleanup_deadline = time.monotonic() + 5
            while process.active_processes() and time.monotonic() < cleanup_deadline:
                time.sleep(0.02)
            clean = process.active_processes() == 0
            for reader in readers:
                reader.result(timeout=max(0.01, cleanup_deadline - time.monotonic()))
            output = log.finish()
            started = process.execution_started()
            output.update(
                {
                    "program": program,
                    "cwd": str(actual_cwd),
                    "exit_code": exit_code if started else None,
                    "process_started": started,
                    "termination_reason": "launch_failed" if started is False else reason,
                    "cleanup_confirmed": clean,
                    "pid": process.pid,
                }
            )
            if started is False and clean:
                return ToolOutcome(
                    data=output,
                    completed=False,
                    error_code=ToolErrorCode.EXECUTION_FAILED,
                    message="Program could not be started",
                )
            interrupted = reason != "exit" or started is not True
            return ToolOutcome(
                data=output,
                completed=not interrupted,
                effect="possible" if clean and not interrupted else "unknown",
                observed_failure=exit_code is not None and exit_code != 0 and not interrupted,
                cancelled=cancellation.is_set(),
                error_code=ToolErrorCode.TIMEOUT
                if reason == "timeout"
                else (ToolErrorCode.UNCERTAIN if interrupted or not clean else None),
            )
        except (OSError, TimeoutError, ValueError):
            return ToolOutcome(
                data=output,
                completed=False,
                effect="unknown" if process else "none",
                cancelled=cancellation.is_set(),
                error_code=ToolErrorCode.EXECUTION_FAILED,
                message="Command lifecycle or output storage failed",
            )
        finally:
            try:
                if process is not None:
                    process.close()
            finally:
                pool.shutdown(wait=True, cancel_futures=True)
                lock.__exit__(None, None, None)

    async def run(args: JsonObject, context: ToolExecutionContext) -> ToolOutcome:
        cancellation = threading.Event()
        task = asyncio.create_task(asyncio.to_thread(execute, args, context, cancellation))
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            cancellation.set()
            return await asyncio.shield(task)

    async def read(args: JsonObject, context: ToolExecutionContext) -> ToolOutcome:
        for session_id in (context.session_id, *context.readable_log_sessions):
            try:
                result = await asyncio.to_thread(
                    logs.read,
                    session_id,
                    require_string(args, "log_id"),
                    require_string(args, "stream"),
                    require_integer(args, "offset", 0),
                    require_integer(args, "length", 16384),
                )
                return ToolOutcome(data={**result, "session_id": session_id})
            except (OSError, ValueError):
                continue
        return ToolOutcome(
            completed=False,
            error_code=ToolErrorCode.INVALID_ARGUMENTS,
            message="Output log unavailable for this session",
        )

    return (
        ToolDefinition(
            "run_command",
            "Run a Windows executable with an argument array after approval. "
            "No implicit shell or bat/cmd. cwd must be inside workspace; environment is host "
            "controlled. Nonzero exit is a completed command with failed verification. "
            "Output is bounded and readable by log ID.",
            object_schema(
                {
                    "program": {"type": "string", "minLength": 1, "maxLength": 4096},
                    "args": {
                        "type": "array",
                        "maxItems": 256,
                        "items": {"type": "string", "maxLength": 24000},
                    },
                    "cwd": {"type": "string", "maxLength": 4096},
                    "timeout_seconds": {"type": "number", "exclusiveMinimum": 0, "maximum": 600},
                },
                ["program"],
            ),
            RiskLevel.EXECUTE,
            run,
            prepare,
            result_schema=command_result,
            manages_timeout=True,
        ),
        ToolDefinition(
            "read_command_output",
            "Read a saved command stream belonging to this session. offset/length are bytes, "
            "UTF-8 replacement decoding. A cleared log returns explicit cleared state.",
            object_schema(
                {
                    "log_id": {"type": "string", "maxLength": 128},
                    "stream": {"enum": ["stdout", "stderr"]},
                    "offset": {"type": "integer", "minimum": 0},
                    "length": {"type": "integer", "minimum": 1, "maximum": 32768},
                },
                ["log_id", "stream"],
            ),
            RiskLevel.READ,
            read,
            result_schema=output_result,
        ),
    )
