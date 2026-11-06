"""External evidence remains identifiable after compaction and reopening the Session."""

import asyncio
import json
import sys
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from jdagent.adapters.fake import FakeApproval
from jdagent.composition import build_runtime
from jdagent.configuration import CliOverrides, resolve_configuration
from jdagent.data_paths import DataPaths
from jdagent.domain.events import MCPPromptPreparedPayload, ToolExecutionCompletedPayload
from jdagent.domain.model import (
    ModelCapabilities,
    ModelEvent,
    ModelRequest,
    ResponseCompleted,
    TextDelta,
    ToolCallCompleted,
)
from jdagent.domain.tools import ApprovalDecision, ToolCall


@pytest.mark.parametrize("engine", ["native", "langgraph"])
@pytest.mark.parametrize("history_turn", ["original", "derived_assistant"])
@pytest.mark.parametrize("history_entry", ["read_history", "search_history"])
@pytest.mark.parametrize("prompt_entry", ["model", "direct"])
def test_host_source_survives_prompt_compaction_and_session_reopen(
    tmp_path: Path, engine: str, history_turn: str, history_entry: str, prompt_entry: str
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
        CliOverrides(provider="fake", runtime_engine=engine),
        data_paths=paths,
        environment={},
    )

    class Model:
        capabilities = ModelCapabilities(context_window=64000)

        def __init__(self) -> None:
            self.requests: list[ModelRequest] = []
            self.sequence = 0
            self.task_calls = 1 if prompt_entry == "direct" else 0
            self.source_session = ""
            self.source_turn = ""

        async def stream(self, request: ModelRequest) -> AsyncIterator[ModelEvent]:
            self.requests.append(request)
            if request.metadata.get("call_kind") == "memory":
                yield TextDelta('{"candidates":[]}')
                yield ResponseCompleted("stop")
                return
            if request.metadata.get("call_kind") == "compaction":
                yield TextDelta(
                    json.dumps(
                        {
                            "goal": "Inspect external material",
                            "facts": [
                                {
                                    "text": "External template: remote-only",
                                    "status": "recorded",
                                    "sources": [self.sequence],
                                    "external_sources": [],
                                }
                            ],
                            "pending": [],
                            "decisions": [],
                            "paths": [],
                            "external_sources": [],
                        }
                    )
                )
                yield ResponseCompleted("stop")
                return
            self.task_calls += 1
            if self.task_calls == 1:
                yield ToolCallCompleted(
                    ToolCall(
                        "template",
                        "get_mcp_prompt",
                        {
                            "server_id": "docs",
                            "name": "explain",
                            "arguments": {"topic": "fixture_large"},
                        },
                    )
                )
                yield ResponseCompleted("tool_calls")
            elif self.task_calls in {2, 6}:
                yield ToolCallCompleted(
                    ToolCall(f"derived-{self.task_calls}", "calculator", {"expression": "1+2"})
                )
                yield ResponseCompleted("tool_calls")
            elif self.task_calls == 5:
                yield ToolCallCompleted(
                    ToolCall(
                        "history",
                        history_entry,
                        {
                            "session_id": self.source_session,
                            "turn_id": self.source_turn,
                        }
                        if history_entry == "read_history"
                        else {"session_id": self.source_session, "query": "external template"},
                    )
                )
                yield ResponseCompleted("tool_calls")
            else:
                yield TextDelta("Used the external template.")
                yield ResponseCompleted("stop")

    async def scenario() -> None:
        model = Model()
        runtime = build_runtime(
            config, FakeApproval(ApprovalDecision.APPROVE), model_wrapper=lambda _: model
        )
        try:
            trigger = (
                "/mcp prompt docs explain topic=fixture_large"
                if prompt_entry == "direct"
                else "User-owned project fact: use pytest. Inspect the template."
            )
            turn = await runtime.coordinator.send(trigger)
            assert turn.result.stop_reason.value == "completed"
            model.source_session, model.source_turn = turn.session_id, turn.turn_id
            event = await anext(
                event
                async for event in runtime.session.read(turn.session_id)
                if isinstance(
                    event.payload,
                    MCPPromptPreparedPayload
                    if prompt_entry == "direct"
                    else ToolExecutionCompletedPayload,
                )
            )
            model.sequence = event.sequence
            result = event.payload
            assert isinstance(result, (ToolExecutionCompletedPayload, MCPPromptPreparedPayload))
            outcome = (
                result.outcome
                if isinstance(result, MCPPromptPreparedPayload)
                else result.result.outcome
            )
            assert outcome is not None
            original = outcome.data["original"]
            compacted = await runtime.coordinator.compact(turn.session_id)
            assert "failed" not in compacted
            maintenance_inputs = [
                request.messages[0].content
                for request in model.requests
                if request.metadata.get("call_kind") == "compaction"
            ]
            assert maintenance_inputs and all(
                "remote-only " * 1500 not in text for text in maintenance_inputs
            )
        finally:
            await runtime.aclose()

        reopened = build_runtime(
            config, FakeApproval(ApprovalDecision.APPROVE), model_wrapper=lambda _: model
        )
        try:
            resumed = await reopened.coordinator.send("Continue", session_id=turn.session_id)
            assert resumed.result.stop_reason.value == "completed"
            if history_turn == "derived_assistant":
                model.source_turn = resumed.turn_id
            request = model.requests[-1]
            summary = json.loads(
                next(
                    part.content
                    for part in request.system_parts
                    if part.source == "history_summary"
                )
            )
            refs = summary["external_sources"]
            assert refs and refs[0]["sequence"] == model.sequence
            assert refs[0]["source"]["server_id"] == "docs"
            assert refs[0]["source"]["capability_kind"] == "prompt"
            assert refs[0]["original"] == original
            assert summary["facts"][0]["external_sources"] == [refs[0]]
            protected = "".join(
                part.content for part in request.system_parts if part.source == "protected_user"
            )
            assert trigger in protected
            assert "remote-only" not in protected
            assert "remote-only " * 100 not in "".join(
                part.content for part in request.system_parts
            )
            await reopened.coordinator.release_session(turn.session_id)
            other = await reopened.coordinator.send("Another independent user statement")
            history = await anext(
                event
                async for event in reopened.session.read(other.session_id)
                if isinstance(event.payload, ToolExecutionCompletedPayload)
                and event.payload.result.call_id == "history"
            )
            assert isinstance(history.payload, ToolExecutionCompletedPayload)
            assert history.payload.result.metadata.get("derived_from_mcp") is True
            assert history.payload.result.outcome is not None
            assert history.payload.result.outcome.data["external_sources"]
            await reopened.coordinator.release_session(other.session_id)
            assert reopened.maintenance is not None
            await reopened.maintenance.run_pending()
            extraction = [
                request.messages[0].content
                for request in model.requests
                if request.metadata.get("call_kind") == "memory"
            ]
            assert any(trigger in text for text in extraction)
            assert all("remote-only" not in text for text in extraction)
            assert all("tool_execution_completed" not in text for text in extraction)
        finally:
            await reopened.aclose()

    asyncio.run(scenario())
