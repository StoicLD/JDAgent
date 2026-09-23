"""Windows Job ownership established before trusted bootstrap runs user code."""

import ctypes
import json
import os
import subprocess
import sys
import tempfile
from ctypes import wintypes
from pathlib import Path
from typing import BinaryIO, cast

# -I -S prevents workspace imports, environment Python paths and site startup.
# Only after the host assigns this waiting bootstrap to a Job is argv supplied.
_BOOTSTRAP = """
import json, sys, subprocess
a = json.loads(sys.stdin.buffer.readline())
try:
    p = subprocess.Popen(a['argv'], stdin=subprocess.DEVNULL)
except OSError as error:
    with open(a['status_file'], 'w', encoding='utf-8') as stream:
        json.dump({'started': False, 'error': type(error).__name__}, stream)
    sys.exit(125)
with open(a['status_file'], 'w', encoding='utf-8') as stream:
    json.dump({'started': True, 'pid': p.pid}, stream)
sys.exit(p.wait())
"""


class _Limits(ctypes.Structure):
    _fields_ = [
        ("process_time", ctypes.c_int64),
        ("job_time", ctypes.c_int64),
        ("flags", wintypes.DWORD),
        ("minimum", ctypes.c_size_t),
        ("maximum", ctypes.c_size_t),
        ("active", wintypes.DWORD),
        ("affinity", ctypes.c_size_t),
        ("priority", wintypes.DWORD),
        ("scheduling", wintypes.DWORD),
    ]


class _ExtendedLimits(ctypes.Structure):
    _fields_ = [
        ("basic", _Limits),
        ("io", ctypes.c_uint64 * 6),
        ("process_memory", ctypes.c_size_t),
        ("job_memory", ctypes.c_size_t),
        ("peak_process_memory", ctypes.c_size_t),
        ("peak_job_memory", ctypes.c_size_t),
    ]


class _Accounting(ctypes.Structure):
    _fields_ = [
        ("times", ctypes.c_int64 * 4),
        ("faults", wintypes.DWORD),
        ("total", wintypes.DWORD),
        ("active", wintypes.DWORD),
        ("terminated", wintypes.DWORD),
    ]


class WindowsCommand:
    """Own one non-breakaway kill-on-close Job, its bootstrap and output pipes."""

    def __init__(
        self,
        argv: list[str],
        cwd: Path,
        environment: dict[str, str],
        status_file: Path | None = None,
    ) -> None:
        if os.name != "nt":
            raise OSError("Command execution requires Windows")
        self._temporary = (
            tempfile.TemporaryDirectory(prefix="jdagent-command-") if status_file is None else None
        )
        self._status_file = (
            status_file or Path(self._temporary.name if self._temporary else "") / "status.json"
        )
        self._kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        self._kernel.CreateJobObjectW.argtypes = [wintypes.LPVOID, wintypes.LPCWSTR]
        self._kernel.CreateJobObjectW.restype = wintypes.HANDLE
        self._kernel.SetInformationJobObject.argtypes = [
            wintypes.HANDLE,
            ctypes.c_int,
            wintypes.LPVOID,
            wintypes.DWORD,
        ]
        self._kernel.SetInformationJobObject.restype = wintypes.BOOL
        self._kernel.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        self._kernel.AssignProcessToJobObject.restype = wintypes.BOOL
        self._kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        self._kernel.OpenProcess.restype = wintypes.HANDLE
        self._kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        self._kernel.CloseHandle.restype = wintypes.BOOL
        self._kernel.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
        self._kernel.TerminateJobObject.restype = wintypes.BOOL
        self._kernel.QueryInformationJobObject.argtypes = [
            wintypes.HANDLE,
            ctypes.c_int,
            wintypes.LPVOID,
            wintypes.DWORD,
            wintypes.LPVOID,
        ]
        self._kernel.QueryInformationJobObject.restype = wintypes.BOOL
        self._job = self._kernel.CreateJobObjectW(None, None)
        if not self._job:
            raise ctypes.WinError(ctypes.get_last_error())
        self._process: subprocess.Popen[bytes] | None = None
        try:
            limits = _ExtendedLimits()
            limits.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            if not self._kernel.SetInformationJobObject(
                self._job, 9, ctypes.byref(limits), ctypes.sizeof(limits)
            ):
                raise ctypes.WinError(ctypes.get_last_error())
            self._process = subprocess.Popen(
                [sys.executable, "-I", "-S", "-c", _BOOTSTRAP],
                cwd=cwd,
                env=environment,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                creationflags=subprocess.CREATE_NO_WINDOW,
                close_fds=True,
            )
            process_handle = self._kernel.OpenProcess(0x0101, False, self._process.pid)
            if not process_handle:
                raise ctypes.WinError(ctypes.get_last_error())
            try:
                if not self._kernel.AssignProcessToJobObject(self._job, process_handle):
                    raise ctypes.WinError(ctypes.get_last_error())
            finally:
                self._kernel.CloseHandle(process_handle)
            assert self._process.stdin is not None
            self._process.stdin.write(
                json.dumps(
                    {"argv": argv, "status_file": str(self._status_file)}, ensure_ascii=True
                ).encode()
                + b"\n"
            )
            self._process.stdin.close()
        except BaseException:
            self.close()
            raise

    @property
    def stdout(self) -> BinaryIO:
        assert self._process is not None and self._process.stdout is not None
        return cast(BinaryIO, self._process.stdout)

    @property
    def stderr(self) -> BinaryIO:
        assert self._process is not None and self._process.stderr is not None
        return cast(BinaryIO, self._process.stderr)

    @property
    def pid(self) -> int:
        assert self._process is not None
        return self._process.pid

    def poll(self) -> int | None:
        assert self._process is not None
        return self._process.poll()

    def execution_started(self) -> bool | None:
        """Read the bootstrap handshake; missing evidence never means verification failed."""
        from jdagent.domain.json import normalize_json, require_object

        try:
            status = require_object(
                normalize_json(json.loads(self._status_file.read_text(encoding="utf-8"))), "status"
            )
        except (OSError, ValueError):
            return None
        value = status.get("started")
        return value if isinstance(value, bool) else None

    def active_processes(self) -> int:
        accounting = _Accounting()
        if not self._kernel.QueryInformationJobObject(
            self._job, 1, ctypes.byref(accounting), ctypes.sizeof(accounting), None
        ):
            raise ctypes.WinError(ctypes.get_last_error())
        return int(accounting.active)

    def terminate(self) -> None:
        if not self._kernel.TerminateJobObject(self._job, 1):
            raise ctypes.WinError(ctypes.get_last_error())

    def close(self) -> None:
        if self._job:
            self._kernel.CloseHandle(self._job)
            self._job = None
        if self._process is not None:
            if self._process.poll() is None:
                self._process.kill()
            self._process.wait(timeout=5)
            for pipe in (self._process.stdin, self._process.stdout, self._process.stderr):
                if pipe is not None:
                    pipe.close()
        if self._temporary is not None:
            self._temporary.cleanup()
