"""Recover real abrupt host exits while an independent HTTP peer stays alive."""

import asyncio
import subprocess
import sys
import threading
from pathlib import Path
from uuid import uuid4

import pytest

from jdagent.adapters.fake import FakeApproval, FakeModelPort
from jdagent.adapters.jsonl_recovery import JsonlRecoveryStore
from jdagent.application.session_recovery import SessionRecovery, prepare_session_resume
from jdagent.composition import build_runtime
from jdagent.data_paths import workspace_identity
from jdagent.domain.events import ToolExecutionCompletedPayload
from jdagent.domain.model import ResponseCompleted
from jdagent.domain.tools import ApprovalDecision, ToolErrorCode
from tests.test_v07_mcp_http import TaskPeerState, wire_server
from tests.test_v07_mcp_task_management import task_configuration


@pytest.mark.parametrize("engine", ["native", "langgraph"])
@pytest.mark.parametrize(
    "cut",
    [
        "before_intent",
        "after_intent",
        "after_send",
        "unregistered_handle",
        "registered",
        "unpublished_result",
        "finished",
    ],
)
def test_abrupt_host_exit_preserves_evidence_and_never_resubmits_remote_work(
    tmp_path: Path, engine: str, cut: str
) -> None:
    arrived, release = threading.Event(), threading.Event()
    state = TaskPeerState(status="completed")
    with wire_server(
        "2026-07-28",
        "json",
        task_state=state,
        tool_gate=(arrived, release) if cut == "after_send" else None,
    ) as url:
        config = task_configuration(tmp_path, url, engine)
        session_id = str(uuid4())
        host = Path(__file__).parent / "fixtures" / "mcp_crashing_client.py"
        process = subprocess.Popen(
            [sys.executable, str(host), engine, cut, session_id, str(tmp_path)],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        try:
            if cut == "after_send":
                assert arrived.wait(15), "The peer did not receive the business request"
                process.terminate()
            stdout, stderr = process.communicate(timeout=20)
            assert process.returncode != 0 if cut == "after_send" else process.returncode == 72, (
                stdout[-2000:] + stderr[-4000:]
            ).decode(errors="replace")
        finally:
            if process.poll() is None:
                process.kill()
                process.communicate(timeout=5)
            release.set()
        assert state.submissions == (0 if cut in {"before_intent", "after_intent"} else 1)

        async def scenario() -> None:
            model = FakeModelPort(scripts=((ResponseCompleted("stop"),),))
            runtime = build_runtime(
                config, FakeApproval(ApprovalDecision.APPROVE), model_wrapper=lambda _: model
            )
            try:
                submissions, queries = state.submissions, state.queries
                resumed = await prepare_session_resume(
                    session=runtime.session,
                    recovery=SessionRecovery(
                        JsonlRecoveryStore(config.session_directory, runtime.session)
                    ),
                    session_id=session_id,
                    workspace_identity=workspace_identity(tmp_path),
                    allow_recovery_snapshot=True,
                )
                assert resumed.session_id == session_id
                events = [event async for event in runtime.session.read(session_id)]
                results = [
                    event.payload.result
                    for event in events
                    if isinstance(event.payload, ToolExecutionCompletedPayload)
                    and event.payload.result.call_id == "remote"
                ]
                assert len(results) == 1
                assert results[0].error_code is (
                    ToolErrorCode.NOT_EXECUTED
                    if cut == "before_intent"
                    else None
                    if cut == "finished"
                    else ToolErrorCode.UNCERTAIN
                )
                tasks = await runtime.coordinator.mcp_tasks(session_id)
                assert (
                    state.submissions == submissions
                    and state.queries == queries
                    and not model.requests
                )
                if cut in {"registered", "unpublished_result", "finished"}:
                    assert len(tasks) == 1 and tasks[0].result_available is (cut == "finished")
                    recovered = await runtime.coordinator.mcp_task(
                        session_id, tasks[0].task_ref, action="result"
                    )
                    assert recovered.error_code is None and "remote task result" in repr(
                        recovered.data
                    )
                    assert state.submissions == submissions and not model.requests
                    assert state.queries == queries + (0 if cut == "finished" else 1)
                else:
                    assert not tasks
            finally:
                await runtime.aclose()

        asyncio.run(scenario())
