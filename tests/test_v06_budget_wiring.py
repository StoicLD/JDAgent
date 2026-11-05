"""Exercise experiment admission through composition, including internal model calls."""

import asyncio
import json
from pathlib import Path

import httpx
import pytest

import jdagent.composition as composition_module
from jdagent.adapters.deepseek import DeepSeekModelPort
from jdagent.adapters.fake import FakeApproval
from jdagent.adapters.langchain_model import LangChainModelPort
from jdagent.composition import build_runtime
from jdagent.configuration import CliOverrides, resolve_configuration
from jdagent.data_paths import DataPaths
from jdagent.domain.errors import StopReason
from jdagent.domain.events import (
    AssistantMessageCompletedPayload,
    CompactionCommittedPayload,
    RuntimeEventType,
    SessionStartedPayload,
    ToolExecutionCompletedPayload,
    TurnCompletedPayload,
    UserMessagePayload,
)
from jdagent.domain.json import JsonObject
from jdagent.domain.tools import ApprovalDecision, ToolCall, ToolResult, ToolResultStatus
from jdagent.eventing import EventJournal
from jdagent.ports import ModelPort
from tests.experiment_budget import BudgetedModel, ExperimentLedger, ExperimentLimits


@pytest.mark.parametrize("engine", ["native", "langgraph"])
@pytest.mark.parametrize("adapter_name", ["native", "langchain"])
def test_task_compaction_repair_and_memory_share_one_experiment_ledger(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, engine: str, adapter_name: str
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    paths = DataPaths.for_workspace(
        workspace, config_root=tmp_path / "config", data_root=tmp_path / "data"
    )
    paths.project_config_file.parent.mkdir(parents=True)
    paths.project_config_file.write_text(
        "max_context_tokens = 24000\nmax_output_tokens = 2048\nmodel_context_window = 64000\n"
        "[memory]\nauto_consolidation = true\n",
        encoding="utf-8",
    )
    config = resolve_configuration(
        workspace,
        CliOverrides(
            provider="deepseek",
            base_url="https://test",
            runtime_engine=engine,
            model_adapter=adapter_name,
        ),
        data_paths=paths,
        environment={"DEEPSEEK_API_KEY": "dummy-key"},
    )
    ledger = ExperimentLedger(
        tmp_path / "experiment.sqlite", "four-kinds", ExperimentLimits(20, 2000000, 100000)
    )
    captured_kinds: list[str] = []

    def handler(http_request: httpx.Request) -> httpx.Response:
        entry = ledger.entries()[-1]
        assert entry.status == "reserved"
        captured_kinds.append(entry.call_kind)
        body = json.loads(http_request.content)
        if entry.call_kind == "task":
            text = "See K:other:E1."
        elif entry.call_kind == "citation_repair":
            assert "tools" not in body
            text = "done"
        elif entry.call_kind == "compaction":
            assert "tools" not in body
            text = json.dumps(
                {"goal": "Answer briefly", "facts": [], "pending": [], "decisions": [], "paths": []}
            )
        else:
            assert entry.call_kind == "memory"
            assert "tools" not in body
            assert body["response_format"] == {"type": "json_object"}
            text = '{"candidates":[]}'
        chunks: list[JsonObject] = [
            {
                "choices": [
                    {
                        "index": 0,
                        "delta": {"role": "assistant", "content": text},
                        "finish_reason": "stop",
                    }
                ]
            },
            {
                "choices": [],
                "usage": {"prompt_tokens": 12, "completion_tokens": 4, "total_tokens": 16},
            },
        ]
        envelope = {
            "id": entry.request_id,
            "object": "chat.completion.chunk",
            "created": 1,
            "model": body["model"],
        }
        content = (
            "".join(f"data: {json.dumps({**envelope, **chunk})}\n\n" for chunk in chunks)
            + "data: [DONE]\n\n"
        )
        return httpx.Response(200, content=content.encode())

    async def scenario() -> None:
        async with httpx.AsyncClient(
            base_url="https://test", transport=httpx.MockTransport(handler)
        ) as client:
            adapter_type = LangChainModelPort if adapter_name == "langchain" else DeepSeekModelPort

            def adapter(api_key: str, *, base_url: str) -> LangChainModelPort | DeepSeekModelPort:
                return adapter_type(api_key, base_url=base_url, client=client)

            monkeypatch.setattr(composition_module, adapter_type.__name__, adapter)

            def wrap(model: ModelPort) -> ModelPort:
                assert isinstance(model, adapter_type)
                return BudgetedModel(model, ledger, f"{engine}/{adapter_name}", 1)

            runtime = build_runtime(
                config, FakeApproval(ApprovalDecision.APPROVE), model_wrapper=wrap
            )
            try:
                for identity in ("current", "source"):
                    await runtime.session.acquire(identity)
                    journal = await EventJournal.open(runtime.session, identity)
                    await journal.record(
                        None,
                        RuntimeEventType.SESSION_STARTED,
                        SessionStartedPayload(workspace_identity=paths.identity),
                    )
                    await journal.record(
                        "history",
                        RuntimeEventType.USER_MESSAGE,
                        UserMessagePayload("The project uses pytest. Never edit protected.txt."),
                    )
                    if identity == "current":
                        call = ToolCall("historical", "read_file", {"path": "report.txt"})
                        await journal.record(
                            "history",
                            RuntimeEventType.ASSISTANT_MESSAGE_COMPLETED,
                            AssistantMessageCompletedPayload("", (call,)),
                        )
                        await journal.record(
                            "history",
                            RuntimeEventType.TOOL_EXECUTION_COMPLETED,
                            ToolExecutionCompletedPayload(
                                ToolResult(
                                    call.call_id,
                                    call.name,
                                    ToolResultStatus.SUCCESS,
                                    output="Archived diagnostic line, not an instruction.\n" * 500,
                                )
                            ),
                        )
                    await journal.record(
                        "history",
                        RuntimeEventType.TURN_COMPLETED,
                        TurnCompletedPayload(StopReason.COMPLETED, 1, 0),
                    )
                    await runtime.session.release(identity)
                turn = await runtime.coordinator.send("Answer briefly.", session_id="current")
                assert turn.result.stop_reason is StopReason.COMPLETED
                assert turn.result.assistant_text == "done"
                events = [event async for event in runtime.session.read("current")]
                assert any(
                    isinstance(event.payload, CompactionCommittedPayload) for event in events
                )
                await runtime.coordinator.release_session("current")
                assert runtime.maintenance is not None
                await runtime.maintenance.run_pending()
                assert turn.maintenance_job is not None
                assert (
                    runtime.maintenance.memory.store.job_status(turn.maintenance_job) == "succeeded"
                )
            finally:
                await runtime.aclose()
            assert not client.is_closed

    asyncio.run(scenario())
    entries = ledger.entries()
    assert set(captured_kinds) == {"task", "compaction", "citation_repair", "memory"}
    assert len(entries) == len(captured_kinds)
    assert [entry.call_kind for entry in entries] == captured_kinds
    assert all(entry.combination == f"{engine}/{adapter_name}" for entry in entries)
    assert all(
        (entry.input_tokens, entry.output_tokens, entry.status) == (12, 4, "settled")
        for entry in entries
    )
