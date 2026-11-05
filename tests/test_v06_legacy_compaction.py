"""Restore v0.5 checkpoints or safely fall back to originals when an object is unusable."""

import asyncio
import hashlib
import json
import shutil
from pathlib import Path

import httpx
import pytest

from jdagent.adapters.jsonl_recovery import JsonlRecoveryStore
from jdagent.application.session_recovery import SessionRecovery, prepare_session_resume
from jdagent.domain.errors import StopReason
from jdagent.domain.events import CompactionCommittedPayload
from tests.runtime_matrix import COMBINATIONS, RuntimeCase, model_response

FIXTURE = Path(__file__).resolve().parents[1] / "testdata" / "v0.5-compacted"


@pytest.mark.parametrize("combination", COMBINATIONS, ids=lambda pair: "/".join(pair))
@pytest.mark.parametrize("state", ["intact", "missing", "corrupt"])
def test_old_checkpoint_and_originals_survive_backend_switch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, combination: tuple[str, str], state: str
) -> None:
    case = RuntimeCase(tmp_path, monkeypatch, combination)
    case.workspace.mkdir()
    manifest = json.loads((FIXTURE / "manifest.json").read_text())
    for name, digest in manifest["files"].items():
        assert hashlib.sha256((FIXTURE / name).read_bytes()).hexdigest() == digest
    shutil.copytree(FIXTURE / "objects", case.paths.sessions_directory / "objects")
    original = (FIXTURE / "v05-compacted.jsonl").read_bytes()
    target = case.paths.sessions_directory / "v05-compacted.jsonl"
    target.write_bytes(original)
    checkpoint = case.paths.sessions_directory / "objects" / manifest["checkpoint"]
    if state == "missing":
        checkpoint.unlink()
    elif state == "corrupt":
        checkpoint.write_bytes(b"not the committed object")
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        body = json.loads(request.content)
        text = "\n".join(message["content"] or "" for message in body["messages"])
        assert "Never edit protected.txt" in text
        if state == "intact":
            assert "Legacy compacted goal" in text and "legacy-report-original" not in text
        else:
            assert "Legacy compacted goal" not in text and "legacy-report-original" in text
        return model_response("Retained the old constraint.")

    async def scenario() -> None:
        for chosen in (combination, ("native", "native")):
            async with case.open(handler, combination=chosen) as runtime:
                prepared = await prepare_session_resume(
                    session=runtime.session,
                    recovery=SessionRecovery(
                        JsonlRecoveryStore(case.paths.sessions_directory, runtime.session)
                    ),
                    session_id="v05-compacted",
                    workspace_identity=case.paths.identity,
                    allow_recovery_snapshot=True,
                )
                assert prepared.session_id == "v05-compacted"
                restored = await runtime.reconstruct_request(manifest["request_manifest"])
                assert any(
                    "Never edit protected.txt" in p.content
                    for p in (*restored.system_parts, *restored.messages)
                )
                turn = await runtime.coordinator.send(
                    "Continue reading only.", session_id=prepared.session_id
                )
                assert turn.result.stop_reason is StopReason.COMPLETED
                assert (turn.result.model_calls, turn.result.tool_calls) == (1, 0)
                events = [e async for e in runtime.session.read(prepared.session_id)]
                assert sum(isinstance(e.payload, CompactionCommittedPayload) for e in events) == 1
                assert target.read_bytes().startswith(original)

    asyncio.run(scenario())
    assert len(requests) == 2
