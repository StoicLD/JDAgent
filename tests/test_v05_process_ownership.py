import asyncio
import subprocess
import sys
from pathlib import Path

import pytest

from jdagent.adapters.jsonl_session import JsonlSession
from jdagent.domain.errors import SessionError, SessionErrorCode


def test_two_processes_cannot_own_same_session_and_crash_releases_it(tmp_path: Path) -> None:
    code = (
        "import asyncio,sys,os; from pathlib import Path; "
        "from jdagent.adapters.jsonl_session import JsonlSession; "
        "session=JsonlSession(Path(sys.argv[1])); "
        "asyncio.run(session.acquire('owned')); print('owned',flush=True); input(); os._exit(23)"
    )
    child = subprocess.Popen(
        [sys.executable, "-c", code, str(tmp_path)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert child.stdout is not None
        assert child.stdout.readline().strip() == "owned"
        session = JsonlSession(tmp_path)
        with pytest.raises(SessionError) as error:
            asyncio.run(session.acquire("owned"))
        assert error.value.code is SessionErrorCode.BUSY
        asyncio.run(session.acquire("different"))
        asyncio.run(session.release("different"))
        assert child.stdin is not None
        child.stdin.write("crash\n")
        child.stdin.flush()
        child.wait(timeout=5)
        assert child.returncode == 23
        asyncio.run(session.acquire("owned"))
        asyncio.run(session.release("owned"))
    finally:
        if child.poll() is None:
            child.terminate()
            child.wait(timeout=5)
        for stream in (child.stdin, child.stdout, child.stderr):
            if stream is not None:
                stream.close()
