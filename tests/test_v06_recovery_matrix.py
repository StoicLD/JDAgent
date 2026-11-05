"""Crash and fallback checks with real journals, locks, runners, and model adapters."""

import asyncio
import subprocess
import sys
from pathlib import Path

import httpx
import pytest

from jdagent.adapters.jsonl_recovery import JsonlRecoveryStore
from jdagent.adapters.jsonl_session import JsonlSession
from jdagent.application.session_recovery import (
    LogicalRecovery,
    SessionRecovery,
    prepare_session_resume,
)
from jdagent.domain.errors import SessionError, StopReason
from jdagent.domain.events import RecoverySnapshotPayload, ToolExecutionStartedPayload
from tests.runtime_matrix import COMBINATIONS, RuntimeCase, model_response


@pytest.mark.parametrize("combination", COMBINATIONS, ids=lambda pair: "/".join(pair))
@pytest.mark.parametrize(
    "phase", ["before_request", "write_started", "result_saved", "turn_completed"]
)
def test_crash_recovery_and_native_fallback_never_replay_effects(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    combination: tuple[str, str],
    phase: str,
) -> None:
    case = RuntimeCase(tmp_path, monkeypatch, combination)
    process = subprocess.run(
        [
            sys.executable,
            "-m",
            "tests.fixtures.v06_forced_exit",
            str(tmp_path),
            *combination,
            phase,
        ],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert process.returncode == 23, process.stderr
    assert process.stdout == process.stderr == ""
    parent_path = case.paths.sessions_directory / "crashed.jsonl"
    original = parent_path.read_bytes()
    effect = case.workspace / "effect.txt"
    expected_effect = b"once" if phase in {"result_saved", "turn_completed"} else None
    assert (effect.read_bytes() if effect.exists() else None) == expected_effect
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return model_response("Inspected existing history without replaying tools.")

    async def scenario() -> None:
        async with case.open(handler) as runtime:
            recovery = SessionRecovery(
                JsonlRecoveryStore(case.paths.sessions_directory, runtime.session)
            )
            result = await recovery.recover("crashed")
            expected = {
                "before_request": LogicalRecovery.INTERRUPTED_SAFE,
                "write_started": LogicalRecovery.UNCERTAIN_SIDE_EFFECT,
                "result_saved": LogicalRecovery.INTERRUPTED_SAFE,
                "turn_completed": LogicalRecovery.CLEAN,
            }[phase]
            assert result.logical is expected
            if phase == "write_started":
                with pytest.raises(SessionError, match="uncertain"):
                    await runtime.coordinator.send("Continue.", session_id="crashed")
                await runtime.coordinator.release_session("crashed")
                assert not requests and parent_path.read_bytes() == original
                with pytest.raises(SessionError, match="uncertain"):
                    await prepare_session_resume(
                        session=runtime.session,
                        recovery=recovery,
                        session_id="crashed",
                        workspace_identity=case.paths.identity,
                        allow_recovery_snapshot=False,
                    )
            prepared = await prepare_session_resume(
                session=runtime.session,
                recovery=recovery,
                session_id="crashed",
                workspace_identity=case.paths.identity,
                allow_recovery_snapshot=True,
            )
            identity = prepared.session_id
            assert (identity != "crashed") is (phase == "write_started")
            before = [e async for e in runtime.session.read(identity)]
            turn = await runtime.coordinator.send(
                "Inspect only; never repeat the write.", session_id=identity
            )
            assert turn.result.stop_reason is StopReason.COMPLETED and turn.result.tool_calls == 0
            after = [e async for e in runtime.session.read(identity)]
            assert after[: len(before)] == before
            assert not any(
                isinstance(e.payload, ToolExecutionStartedPayload) for e in after[len(before) :]
            )
            if phase == "write_started":
                assert any(isinstance(e.payload, RecoverySnapshotPayload) for e in after)
                assert parent_path.read_bytes() == original
            else:
                assert parent_path.read_bytes().startswith(original)
            preserved = runtime.session.path_for(identity).read_bytes()
        # Reopen precisely the same data using the fallback combination.
        async with case.open(handler, combination=("native", "native")) as fallback:
            prepared = await prepare_session_resume(
                session=fallback.session,
                recovery=SessionRecovery(
                    JsonlRecoveryStore(case.paths.sessions_directory, fallback.session)
                ),
                session_id=identity,
                workspace_identity=case.paths.identity,
                allow_recovery_snapshot=True,
            )
            assert prepared.session_id == identity
            assert fallback.session.path_for(identity).read_bytes() == preserved
            turn = await fallback.coordinator.send(
                "Inspect again; do not write.", session_id=identity
            )
            assert turn.result.stop_reason is StopReason.COMPLETED and turn.result.tool_calls == 0
            assert fallback.session.path_for(identity).read_bytes().startswith(preserved)
        competitor = JsonlSession(case.paths.sessions_directory)
        for name in ("crashed", identity):
            await competitor.acquire(name)
            await competitor.release(name)

    asyncio.run(scenario())
    assert len(requests) == 2
    assert (effect.read_bytes() if effect.exists() else None) == expected_effect
    if phase == "write_started":
        assert parent_path.read_bytes() == original
