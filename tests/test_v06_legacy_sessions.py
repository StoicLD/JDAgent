"""Consume actual frozen v0.5 serialization without rewriting the historical prefix."""

import asyncio
import hashlib
import json
from pathlib import Path

import httpx
import pytest

from jdagent.adapters.jsonl_recovery import JsonlRecoveryStore
from jdagent.application.session_recovery import SessionRecovery, prepare_session_resume
from jdagent.domain.errors import SessionError, StopReason
from jdagent.domain.events import RecoverySnapshotPayload, ToolExecutionStartedPayload
from tests.runtime_matrix import COMBINATIONS, RuntimeCase, model_response

FIXTURE = Path(__file__).resolve().parents[1] / "testdata" / "v0.5-sessions"


@pytest.mark.parametrize("combination", COMBINATIONS, ids=lambda pair: "/".join(pair))
@pytest.mark.parametrize("mode", ["complete", "safe", "unknown_started", "unknown_result"])
def test_v05_session_copy_opens_in_selected_backend_and_native_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, combination: tuple[str, str], mode: str
) -> None:
    case = RuntimeCase(tmp_path, monkeypatch, combination)
    case.workspace.mkdir()
    identity = f"v05-{mode}"
    original = (FIXTURE / f"{identity}.jsonl").read_bytes()
    manifest = json.loads((FIXTURE / "manifest.json").read_text())
    assert hashlib.sha256(original).hexdigest() == manifest["files"][f"{identity}.jsonl"]
    case.paths.sessions_directory.mkdir(parents=True)
    parent_path = case.paths.sessions_directory / f"{identity}.jsonl"
    parent_path.write_bytes(original)
    effect = case.workspace / "effect.txt"
    effect.write_bytes(b"existing effect; never repeat")
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert "Never edit protected.txt" in request.content.decode()
        if mode.startswith("unknown"):
            assert "Never automatically replay" in request.content.decode()
            assert "Unconfirmed gap message" not in request.content.decode()
        return model_response("Preserved the original constraint and existing write.")

    async def scenario() -> None:
        async with case.open(handler) as runtime:
            recovery = SessionRecovery(
                JsonlRecoveryStore(case.paths.sessions_directory, runtime.session)
            )
            if mode.startswith("unknown"):
                with pytest.raises(SessionError, match="uncertain"):
                    await runtime.coordinator.send("continue", session_id=identity)
                await runtime.coordinator.release_session(identity)
                assert not requests
            prepared = await prepare_session_resume(
                session=runtime.session,
                recovery=recovery,
                session_id=identity,
                workspace_identity=case.paths.identity,
                allow_recovery_snapshot=True,
            )
            resumed = prepared.session_id
            before = [e async for e in runtime.session.read(resumed)]
            if mode.startswith("unknown"):
                snapshots = [
                    e.payload for e in before if isinstance(e.payload, RecoverySnapshotPayload)
                ]
                assert len(snapshots) == 1 and len(snapshots[0].unknown_operations) == 1
                operation = json.loads(snapshots[0].unknown_operations[0])
                assert operation["call_id"] == "old-write" and operation["effect"] == "unknown"
            turn = await runtime.coordinator.send(
                "Read history without running tools.", session_id=resumed
            )
            assert turn.result.stop_reason is StopReason.COMPLETED and turn.result.tool_calls == 0
            after = [e async for e in runtime.session.read(resumed)]
            assert after[: len(before)] == before
            assert not any(
                isinstance(e.payload, ToolExecutionStartedPayload) for e in after[len(before) :]
            )
            preserved = runtime.session.path_for(resumed).read_bytes()
        async with case.open(handler, combination=("native", "native")) as runtime:
            assert runtime.session.path_for(resumed).read_bytes() == preserved
            prepared = await prepare_session_resume(
                session=runtime.session,
                recovery=SessionRecovery(
                    JsonlRecoveryStore(case.paths.sessions_directory, runtime.session)
                ),
                session_id=resumed,
                workspace_identity=case.paths.identity,
                allow_recovery_snapshot=True,
            )
            assert prepared.session_id == resumed
            turn = await runtime.coordinator.send("Read history again.", session_id=resumed)
            assert turn.result.stop_reason is StopReason.COMPLETED and turn.result.tool_calls == 0
            assert runtime.session.path_for(resumed).read_bytes().startswith(preserved)

    asyncio.run(scenario())
    assert len(requests) == 2
    assert effect.read_bytes() == b"existing effect; never repeat"
    assert parent_path.read_bytes().startswith(original)
    if mode.startswith("unknown"):
        assert parent_path.read_bytes() == original
