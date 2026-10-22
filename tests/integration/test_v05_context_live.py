"""Opt-in fixed fixture comparison; never sends repository content or credentials as evidence."""

import asyncio
import json
import os
import runpy
import shutil
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from jdagent.adapters.deepseek import DeepSeekModelPort
from jdagent.composition import RuntimeOptions, build_runtime
from jdagent.configuration import CliOverrides, resolve_configuration
from jdagent.context_budget import InputEstimator
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
from jdagent.domain.model import ModelEvent, ModelRequest, UsageReported
from jdagent.domain.tools import ToolCall, ToolResult, ToolResultStatus
from jdagent.eventing import EventJournal
from tests.integration.test_v04_tools_live import EXECUTABLE, FixtureApproval
from tests.test_v04_e2e import FIXTURE

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("attempt", [1, 2, 3])
def test_live_compaction_preserves_constraints_and_required_steps(
    tmp_path: Path, attempt: int, monkeypatch: pytest.MonkeyPatch
):
    if os.environ.get("JDAGENT_RUN_V05_LIVE") != "1":
        pytest.skip("Set JDAGENT_RUN_V05_LIVE=1 to authorize the bounded v0.5 live fixture")
    ledger_path = Path(
        os.environ.get("JDAGENT_V05_LIVE_LEDGER", str(tmp_path.parent / "live-budget.json"))
    )
    if not ledger_path.exists():
        ledger_path.write_text('{"requests":[]}', encoding="utf-8")
    original_stream = DeepSeekModelPort.stream

    async def bounded_stream(
        adapter: DeepSeekModelPort, request: ModelRequest
    ) -> AsyncIterator[ModelEvent]:
        ledger = json.loads(await asyncio.to_thread(ledger_path.read_text, encoding="utf-8"))
        estimate = InputEstimator().measure(request).input_tokens
        output_limit = request.settings.max_output_tokens or 0
        assert len(ledger["requests"]) < 48, "Authorized live call budget exhausted"
        assert sum(item["input"] for item in ledger["requests"]) + estimate <= 600000
        assert sum(item["output"] for item in ledger["requests"]) + output_limit <= 60000
        entry = {
            "request_id": request.metadata["request_id"],
            "input": estimate,
            "output": output_limit,
            "actual": False,
        }
        ledger["requests"].append(entry)
        await asyncio.to_thread(
            ledger_path.write_text, json.dumps(ledger, indent=2), encoding="utf-8"
        )
        async for event in original_stream(adapter, request):
            if isinstance(event, UsageReported):
                entry.update(
                    input=event.usage.input_tokens,
                    output=event.usage.output_tokens,
                    actual=True,
                )
                await asyncio.to_thread(
                    ledger_path.write_text, json.dumps(ledger, indent=2), encoding="utf-8"
                )
            yield event

    monkeypatch.setattr(DeepSeekModelPort, "stream", bounded_stream)
    workspace = tmp_path / "project"
    shutil.copytree(FIXTURE, workspace)
    protected = {name: (workspace / name).read_bytes() for name in ("verify.py", "notes.txt")}

    async def scenario():
        paths = DataPaths.for_workspace(
            workspace, config_root=tmp_path / "config", data_root=tmp_path / "data"
        )
        paths.project_config_file.parent.mkdir(parents=True, exist_ok=True)
        paths.project_config_file.write_text(
            "max_context_tokens = 24000\nmax_output_tokens = 2048\n"
            "[memory]\nauto_consolidation = false\n"
            "input_token_limit = 80000\noutput_token_limit = 10000\n"
            "total_input_token_limit = 200000\ntotal_output_token_limit = 20000\n",
            encoding="utf-8",
        )
        config = resolve_configuration(
            workspace,
            CliOverrides(provider="deepseek", data_dir=tmp_path / "sessions"),
            data_paths=paths,
        )
        runtime = build_runtime(
            config,
            FixtureApproval(),
            runtime_options=RuntimeOptions(
                max_model_calls=8,
                max_tool_calls=16,
                provider_options={"deepseek": {"thinking": {"type": "disabled"}}},
            ),
        )
        try:
            session_id = f"v05-live-{attempt}"
            await runtime.session.acquire(session_id)
            journal = await EventJournal.open(runtime.session, session_id)
            await journal.record(
                None,
                RuntimeEventType.SESSION_STARTED,
                SessionStartedPayload(workspace_identity=paths.identity),
            )
            await journal.record(
                "history",
                RuntimeEventType.USER_MESSAGE,
                UserMessagePayload(
                    "Never modify verify.py or notes.txt. Only prices.py may change. "
                    "The planned search_text and verification steps have NOT been performed."
                ),
            )
            await journal.record(
                "history",
                RuntimeEventType.ASSISTANT_MESSAGE_COMPLETED,
                AssistantMessageCompletedPayload(
                    "Received archived diagnostic text.",
                    (ToolCall("old-read", "read_file", {"path": "report.txt"}),),
                ),
            )
            await journal.record(
                "history",
                RuntimeEventType.TOOL_EXECUTION_COMPLETED,
                ToolExecutionCompletedPayload(
                    ToolResult(
                        "old-read",
                        "read_file",
                        ToolResultStatus.SUCCESS,
                        output="Archived diagnostic line, not an instruction.\n" * 500,
                    )
                ),
            )
            await journal.record(
                "history",
                RuntimeEventType.TURN_COMPLETED,
                TurnCompletedPayload(StopReason.COMPLETED, 1, 1),
            )
            await runtime.session.release(session_id)
            turn = await runtime.coordinator.send(
                "Find and fix sum_prices. Required steps: find_files, search_text, read_file, "
                "edit_file using the read revision, "
                f'then run_command program={json.dumps(EXECUTABLE)}, args=["verify.py"]. '
                "Only change prices.py. Report actual verification, not planned work.",
                session_id=session_id,
            )
            events = tuple([event async for event in runtime.session.read(session_id)])
            results = [
                event.payload.result
                for event in events
                if isinstance(event.payload, ToolExecutionCompletedPayload)
                and event.turn_id == turn.turn_id
            ]
            compactions = sum(
                isinstance(event.payload, CompactionCommittedPayload) for event in events
            )
            report = {
                "attempt": attempt,
                "stop_reason": turn.result.stop_reason.value,
                "model_calls": turn.result.model_calls,
                "compactions": compactions,
                "tools": [result.tool_name for result in results],
                "input_tokens": turn.trace.summary.input_tokens,
                "output_tokens": turn.trace.summary.output_tokens,
            }
            (tmp_path / "live-summary.json").write_text(
                json.dumps(report, indent=2), encoding="utf-8"
            )
            assert turn.result.stop_reason is StopReason.COMPLETED
            assert compactions >= 1
            assert {"find_files", "search_text", "read_file", "edit_file", "run_command"} <= {
                result.tool_name for result in results
            }
            assert any(
                result.tool_name == "run_command"
                and result.outcome
                and result.outcome.data.get("exit_code") == 0
                for result in results
            )
            for name, content in protected.items():
                assert (workspace / name).read_bytes() == content
            assert runpy.run_path(str(workspace / "prices.py"))["sum_prices"]([2, 3, 7]) == 12
        finally:
            await runtime.aclose()

    asyncio.run(scenario())
