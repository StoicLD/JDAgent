"""Recovery descendants retain external sources without feeding them to memory extraction."""

import asyncio
import json
import sys
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from jdagent.adapters.fake import FakeApproval
from jdagent.adapters.jsonl_recovery import JsonlRecoveryStore
from jdagent.application.session_recovery import SessionRecovery, prepare_session_resume
from jdagent.composition import build_runtime
from jdagent.configuration import CliOverrides, resolve_configuration
from jdagent.data_paths import DataPaths, workspace_identity
from jdagent.domain.events import (
    AssistantMessageCompletedPayload,
    MCPPromptPreparedPayload,
    RuntimeEventType,
    ToolExecutionCompletedPayload,
    ToolExecutionStartedPayload,
)
from jdagent.domain.model import (
    ModelCapabilities,
    ModelEvent,
    ModelRequest,
    ResponseCompleted,
    TextDelta,
    ToolCallCompleted,
)
from jdagent.domain.tools import ApprovalDecision, RiskLevel, ToolCall
from jdagent.eventing import EventJournal


@pytest.mark.parametrize("compact_parent", [False, True])
@pytest.mark.parametrize("child_tool", [False, True])
def test_recovery_child_and_nested_child_keep_mcp_provenance_after_restart(
    tmp_path: Path, compact_parent: bool, child_tool: bool
) -> None:
    paths = DataPaths.for_workspace(
        tmp_path, config_root=tmp_path / "config", data_root=tmp_path / "data"
    )
    paths.user_config_file.parent.mkdir(parents=True)
    peer = Path(__file__).parent / "fixtures" / "mcp_stdio_server.py"
    paths.user_config_file.write_text(
        "[memory]\nauto_consolidation = true\n"
        '[mcp.servers.docs]\nenabled = true\ntransport = "stdio"\n'
        f"command = {json.dumps(sys.executable)}\n"
        f"args = {json.dumps([str(peer), '2026-07-28', 'catalog_once'])}\n"
        "prompts = true\n",
        encoding="utf-8",
    )
    config = resolve_configuration(
        tmp_path,
        CliOverrides(provider="fake"),
        data_paths=paths,
        environment={},
    )

    class Model:
        capabilities = ModelCapabilities(context_window=64000)

        def __init__(self) -> None:
            self.requests: list[ModelRequest] = []
            self.action = "answer"
            self.source_sequence = 0
            self.source_session = ""
            self.source_turn = ""

        async def stream(self, request: ModelRequest) -> AsyncIterator[ModelEvent]:
            self.requests.append(request)
            kind = request.metadata.get("call_kind")
            if kind == "memory":
                yield TextDelta('{"candidates":[]}')
            elif kind == "compaction":
                yield TextDelta(
                    json.dumps(
                        {
                            "goal": "Read the external template",
                            "facts": [
                                {
                                    "text": "External template: remote-only",
                                    "status": "recorded",
                                    "sources": [self.source_sequence],
                                }
                            ],
                            "pending": [],
                            "decisions": [],
                            "paths": [],
                        }
                    )
                )
            elif self.action in {"calculator", "history"}:
                call = (
                    ToolCall("derived", "calculator", {"expression": "1+2"})
                    if self.action == "calculator"
                    else ToolCall(
                        "history",
                        "read_history",
                        {"session_id": self.source_session, "turn_id": self.source_turn},
                    )
                )
                self.action = "answer"
                yield ToolCallCompleted(call)
                yield ResponseCompleted("tool_calls")
                return
            else:
                yield TextDelta("Used the external template: remote-only.")
            yield ResponseCompleted("stop")

    async def scenario() -> None:
        model = Model()
        runtime = build_runtime(
            config, FakeApproval(ApprovalDecision.APPROVE), model_wrapper=lambda _: model
        )
        try:
            parent = await runtime.coordinator.send("/mcp prompt docs explain topic=fixture_large")
            template = await anext(
                event
                async for event in runtime.session.read(parent.session_id)
                if isinstance(event.payload, MCPPromptPreparedPayload)
            )
            model.source_sequence = template.sequence
            if compact_parent:
                assert "failed" not in await runtime.coordinator.compact(parent.session_id)
            source_id = parent.session_id
            for generation in range(2):
                await runtime.coordinator.release_session(source_id)
                journal = await EventJournal.open(runtime.session, source_id, require_existing=True)
                await journal.record(
                    f"interrupted-{generation}",
                    RuntimeEventType.TOOL_EXECUTION_STARTED,
                    ToolExecutionStartedPayload(
                        f"local-write-{generation}", "write_text_file", RiskLevel.WRITE
                    ),
                )
                recovered = await prepare_session_resume(
                    session=runtime.session,
                    recovery=SessionRecovery(
                        JsonlRecoveryStore(config.session_directory, runtime.session)
                    ),
                    session_id=source_id,
                    workspace_identity=workspace_identity(tmp_path),
                    allow_recovery_snapshot=True,
                )
                assert recovered.session_id != source_id
                model.action = "calculator" if child_tool else "answer"
                start = len(model.requests)
                turn = await runtime.coordinator.send(
                    "Independent user fact: the project uses pytest.",
                    session_id=recovered.session_id,
                )
                assert turn.result.stop_reason.value == "completed"
                request = model.requests[start]
                assert request.metadata.get("external_context") == "mcp"
                history = "".join(
                    part.content
                    for part in request.system_parts
                    if part.source == "recovery_history"
                )
                assert '"server_id":"docs"' in history or '"server_id": "docs"' in history
                assert "remote-only " * 1500 not in history
                assert any(part.source == "mcp_material_boundary" for part in request.system_parts)
                if child_tool:
                    result = await anext(
                        event.payload.result
                        async for event in runtime.session.read(turn.session_id)
                        if isinstance(event.payload, ToolExecutionCompletedPayload)
                    )
                    assert result.metadata.get("derived_from_mcp") is True
                if compact_parent:
                    events = [event async for event in runtime.session.read(turn.session_id)]
                    model.source_sequence = next(
                        event.sequence
                        for event in reversed(events)
                        if isinstance(event.payload, AssistantMessageCompletedPayload)
                    )
                    assert "failed" not in await runtime.coordinator.compact(turn.session_id)
                source_id = turn.session_id
                model.source_session, model.source_turn = source_id, turn.turn_id
        finally:
            await runtime.aclose()

        reopened = build_runtime(
            config, FakeApproval(ApprovalDecision.APPROVE), model_wrapper=lambda _: model
        )
        try:
            model.action = "history"
            other = await reopened.coordinator.send("Read the recovery answer")
            history_result = await anext(
                event.payload.result
                async for event in reopened.session.read(other.session_id)
                if isinstance(event.payload, ToolExecutionCompletedPayload)
                and event.payload.result.call_id == "history"
            )
            assert history_result.metadata.get("derived_from_mcp") is True
            assert history_result.outcome is not None
            assert history_result.outcome.data["external_sources"]
            await reopened.coordinator.release_session(other.session_id)
            assert reopened.maintenance is not None
            await reopened.maintenance.run_pending()
            extraction = [
                request.messages[0].content
                for request in model.requests
                if request.metadata.get("call_kind") == "memory"
            ]
            assert any("Independent user fact" in text for text in extraction)
            assert all("remote-only" not in text for text in extraction)
            assert all("tool_execution_completed" not in text for text in extraction)
        finally:
            await reopened.aclose()

    asyncio.run(scenario())
