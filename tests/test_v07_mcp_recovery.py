"""Crash-prefix evidence through the composed runner and application resume use case."""

import asyncio
import json
import sys
from pathlib import Path

import pytest

from jdagent.adapters.fake import FakeApproval, FakeModelPort
from jdagent.adapters.jsonl_recovery import JsonlRecoveryStore
from jdagent.application.session_recovery import SessionRecovery, prepare_session_resume
from jdagent.composition import build_runtime
from jdagent.configuration import CliOverrides, resolve_configuration
from jdagent.data_paths import DataPaths, workspace_identity
from jdagent.domain.events import (
    MCPInvocationFinishedPayload,
    MCPInvocationStartedPayload,
    MCPTaskRegisteredPayload,
    ToolExecutionCompletedPayload,
)
from jdagent.domain.model import ResponseCompleted, TextDelta, ToolCallCompleted
from jdagent.domain.tools import ApprovalDecision, ToolCall, ToolErrorCode, ToolResultStatus


@pytest.mark.parametrize(
    "cut",
    [
        "before_intent",
        "after_intent",
        "after_result",
        "before_task_registration",
        "after_task_registration",
    ],
)
def test_resume_uses_durable_mcp_evidence_without_replaying_business(
    tmp_path: Path, cut: str
) -> None:
    paths = DataPaths.for_workspace(
        tmp_path, config_root=tmp_path / "config", data_root=tmp_path / "data"
    )
    paths.user_config_file.parent.mkdir(parents=True)
    peer = Path(__file__).parent / "fixtures" / "mcp_stdio_server.py"
    paths.user_config_file.write_text(
        "[memory]\nauto_consolidation = false\n"
        '[mcp.servers.docs]\nenabled = true\ntransport = "stdio"\n'
        f"command = {json.dumps(sys.executable)}\n"
        f"args = {json.dumps([str(peer), '2026-07-28', 'catalog_once'])}\n"
        'tools = ["read"]\n',
        encoding="utf-8",
    )
    config = resolve_configuration(
        tmp_path,
        CliOverrides(provider="fake"),
        data_paths=paths,
        environment={},
    )
    model = FakeModelPort(
        scripts=(
            (
                ToolCallCompleted(ToolCall("find", "search_tools", {"query": "read"})),
                ResponseCompleted("tool_calls"),
            ),
            (
                ToolCallCompleted(
                    ToolCall(
                        "remote",
                        "mcp__docs__read",
                        {"fixture_task": "normal"} if "task_registration" in cut else {},
                    )
                ),
                ResponseCompleted("tool_calls"),
            ),
            (TextDelta("done"), ResponseCompleted("stop")),
        )
    )

    async def scenario() -> None:
        runtime = build_runtime(
            config, FakeApproval(ApprovalDecision.APPROVE), model_wrapper=lambda _: model
        )
        try:
            turn = await runtime.coordinator.send("Read the remote file")
            assert turn.result.stop_reason.value == "completed"
            events = [event async for event in runtime.session.read(turn.session_id)]
            intent = next(
                event
                for event in events
                if isinstance(event.payload, MCPInvocationStartedPayload)
                and event.payload.call_id == "remote"
            )
            finished = next(
                event
                for event in events
                if isinstance(event.payload, MCPInvocationFinishedPayload)
                and event.payload.result.call_id == "remote"
            )
        finally:
            await runtime.aclose()
        cutoff = (
            intent.sequence - 1
            if cut == "before_intent"
            else intent.sequence
            if cut == "after_intent"
            else finished.sequence
        )
        task_ref: str | None = None
        if "task_registration" in cut:
            registration = next(
                e for e in events if isinstance(e.payload, MCPTaskRegisteredPayload)
            )
            assert isinstance(registration.payload, MCPTaskRegisteredPayload)
            cutoff = registration.sequence - (1 if cut == "before_task_registration" else 0)
            task_ref = registration.payload.task_ref
        path = runtime.session.path_for(turn.session_id)
        path.write_bytes(b"".join(path.read_bytes().splitlines(keepends=True)[:cutoff]))
        resumed = await prepare_session_resume(
            session=runtime.session,
            recovery=SessionRecovery(JsonlRecoveryStore(config.session_directory, runtime.session)),
            session_id=turn.session_id,
            workspace_identity=workspace_identity(tmp_path),
            allow_recovery_snapshot=True,
        )
        assert resumed.session_id == turn.session_id
        recovered = [event async for event in runtime.session.read(turn.session_id)]
        results = [
            event.payload.result
            for event in recovered
            if isinstance(event.payload, ToolExecutionCompletedPayload)
            and event.payload.result.call_id == "remote"
        ]
        assert len(results) == 1
        result = results[0]
        assert result.outcome is not None
        if cut == "before_intent":
            assert result.error_code is ToolErrorCode.NOT_EXECUTED
        elif cut in {"after_intent", "before_task_registration", "after_task_registration"}:
            assert result.error_code is ToolErrorCode.UNCERTAIN
            assert result.outcome.effect == "none" and result.stop_turn
            if cut == "after_task_registration":
                assert result.outcome.data["task_ref"] == task_ref
                assert result.outcome.data["task_status"] == "working"
            elif cut == "before_task_registration":
                assert "task_ref" not in result.outcome.data
        else:
            assert result.status is ToolResultStatus.SUCCESS
            assert result.outcome.data["preview"] == {
                "resultType": "complete",
                "content": [{"type": "text", "text": "ok"}],
            }
        assert len(model.requests) == 3
        assert sum(
            isinstance(event.payload, MCPInvocationStartedPayload)
            and event.payload.call_id == "remote"
            for event in recovered
        ) == (0 if cut == "before_intent" else 1)

    asyncio.run(scenario())
