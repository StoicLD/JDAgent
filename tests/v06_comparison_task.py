"""The fixed tool/compaction task shared by offline rehearsal and v0.6 live comparison.

The caller owns experiment admission, the selected sample slot, and authorization.
This module always requires an explicit key; it never discovers user credentials.
"""

import asyncio
import hashlib
import json
import runpy
import shutil
import sys
import time
from collections import Counter
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import cast

from jdagent.application.headless import run_headless
from jdagent.composition import RuntimeOptions, build_runtime
from jdagent.configuration import CliOverrides, resolve_configuration
from jdagent.data_paths import DataPaths
from jdagent.domain.errors import StopReason
from jdagent.domain.events import (
    AssistantMessageCompletedPayload,
    CompactionCommittedPayload,
    RuntimeEventType,
    SessionStartedPayload,
    ToolExecutionCompletedPayload,
    ToolExecutionStartedPayload,
    TurnCompletedPayload,
    UserMessagePayload,
)
from jdagent.domain.json import JsonObject, normalize_json, require_object
from jdagent.domain.model import ModelEvent, TextDelta
from jdagent.domain.tools import (
    ApprovalDecision,
    ApprovalOutcome,
    ApprovalRequest,
    ToolCall,
    ToolResult,
    ToolResultStatus,
)
from jdagent.eventing import EventJournal
from jdagent.ports import ModelPort
from tests.experiment_budget import BudgetedModel, ExperimentLedger

FIXTURE = Path(__file__).resolve().parents[1] / "testdata" / "v0.4-tools"
FIXTURE_FILES = ("DATASET.md", "notes.txt", "prices.py", "verify.py")
EXECUTABLE = str(Path(sys.executable).resolve())
CONSTRAINT = (
    "Never modify verify.py or notes.txt. Only prices.py may change. "
    "The planned search_text and verification steps have NOT been performed."
)
PROMPT = (
    "Find and fix sum_prices. Required steps: find_files, search_text, read_file, "
    "edit_file using the read revision, "
    f'then run_command program={json.dumps(EXECUTABLE)}, args=["verify.py"]. '
    "Only change prices.py. Report actual verification, not planned work."
)
PROJECT_CONFIG = (
    "model_context_window = 64000\nmax_context_tokens = 24000\nmax_output_tokens = 2048\n"
    "model_timeout_seconds = 120\n"
    "[memory]\nauto_consolidation = false\nheadless_drain_seconds = 0\n"
    "input_token_limit = 80000\noutput_token_limit = 10000\n"
    "total_input_token_limit = 200000\ntotal_output_token_limit = 20000\n"
)


@dataclass(frozen=True)
class Sample:
    label: str
    engine: str
    adapter: str
    attempt: int

    @property
    def combination(self) -> str:
        return f"{self.engine}/{self.adapter}"


SAMPLES = tuple(
    Sample(f"{group}{attempt}", engine, adapter, attempt)
    for group, engine, adapter in (("B", "native", "native"), ("T", "langgraph", "langchain"))
    for attempt in (1, 2, 3)
)


def fixture_fingerprint() -> JsonObject:
    """Fingerprint public synthetic inputs and every fixed task setting."""
    return {
        "files": {
            name: hashlib.sha256((FIXTURE / name).read_bytes()).hexdigest()
            for name in FIXTURE_FILES
        },
        "constraint": CONSTRAINT,
        "prompt": PROMPT,
        "project_config": PROJECT_CONFIG,
        "max_model_calls": 8,
        "max_tool_calls": 16,
        "temperature": 0,
        "thinking": "disabled",
        "archived_line_count": 500,
        "archived_line": "Archived diagnostic line, not an instruction.\n",
    }


def write_json(path: Path, value: JsonObject) -> None:
    """Create evidence once; callers must not overwrite a previous attempt."""
    with path.open("x", encoding="utf-8", newline="\n") as stream:
        stream.write(json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n")
        stream.flush()
        import os

        os.fsync(stream.fileno())


class FixtureApproval:
    async def request(self, request: ApprovalRequest) -> ApprovalOutcome:
        allowed = (request.tool_name == "edit_file" and request.target == "prices.py") or (
            request.tool_name == "run_command"
            and request.display.get("program") == EXECUTABLE
            and request.display.get("args") == ["verify.py"]
        )
        return ApprovalOutcome(ApprovalDecision.APPROVE if allowed else ApprovalDecision.REJECT)


class FirstText:
    def __init__(self) -> None:
        self.started = 0.0
        self.milliseconds: float | None = None

    async def observe(self, event: ModelEvent) -> None:
        if isinstance(event, TextDelta) and event.text and self.milliseconds is None:
            self.milliseconds = (time.perf_counter() - self.started) * 1000


def _prepare(root: Path) -> tuple[Path, DataPaths]:
    workspace = root / "workspace"
    workspace.mkdir()
    for name in FIXTURE_FILES:
        shutil.copyfile(FIXTURE / name, workspace / name)
    paths = DataPaths.for_workspace(workspace, config_root=root / "config", data_root=root / "data")
    paths.project_config_file.parent.mkdir(parents=True)
    paths.project_config_file.write_text(PROJECT_CONFIG, encoding="utf-8", newline="\n")
    return workspace, paths


def _verify_files(workspace: Path) -> JsonObject:
    protected = all(
        (workspace / name).read_bytes() == (FIXTURE / name).read_bytes()
        for name in ("DATASET.md", "notes.txt", "verify.py")
    )
    changed = [
        name
        for name in FIXTURE_FILES
        if (workspace / name).read_bytes() != (FIXTURE / name).read_bytes()
    ]
    namespace = runpy.run_path(str(workspace / "prices.py"))
    sum_prices = cast(Callable[[list[int]], int], namespace["sum_prices"])
    correct = all(
        sum_prices(values) == expected
        for values, expected in (([2, 3, 7], 12), ([], 0), ([-1, 4], 3))
    )
    return require_object(
        normalize_json(
            {
                "protected_unchanged": protected,
                "changed_fixture_files": changed,
                "correct_sums": correct,
            }
        ),
        "file checks",
    )


async def run_fixed_task(
    root: Path,
    sample: Sample,
    ledger: ExperimentLedger,
    *,
    model: str,
    base_url: str,
    api_key: str,
) -> JsonObject:
    """Run exactly one claimed sample and preserve metrics on ordinary failures.

    `root` must already be a fresh, exclusively claimed sample directory. The result
    file is created only once. Cancellation propagates after writing its outcome;
    process death leaves the caller's durable claim and ledger reservations intact.
    """
    if not api_key.strip():
        raise ValueError("An explicit experiment credential is required")
    if sample not in SAMPLES:
        raise ValueError("Only the six fixed comparison samples are allowed")
    report: JsonObject = {
        "sample": sample.label,
        "combination": sample.combination,
        "status": "error",
    }
    clock = FirstText()
    start = time.perf_counter()
    try:
        workspace, paths = await asyncio.to_thread(_prepare, root)
        config = await asyncio.to_thread(
            resolve_configuration,
            workspace,
            CliOverrides(
                provider="deepseek",
                model=model,
                base_url=base_url,
                runtime_engine=sample.engine,
                model_adapter=sample.adapter,
                data_dir=root / "sessions",
            ),
            data_paths=paths,
            environment={"DEEPSEEK_API_KEY": api_key},
        )

        def wrap(port: ModelPort) -> ModelPort:
            return BudgetedModel(port, ledger, sample.combination, sample.attempt)

        runtime = build_runtime(
            config,
            FixtureApproval(),
            runtime_options=RuntimeOptions(
                max_model_calls=8,
                max_tool_calls=16,
                temperature=0,
                provider_options={"deepseek": {"thinking": {"type": "disabled"}}},
            ),
            model_wrapper=wrap,
            model_event_observers=(clock,),
        )
        report["setup_ms"] = (time.perf_counter() - start) * 1000
        try:
            session_id = f"v06-{sample.label}"
            await runtime.session.acquire(session_id)
            journal = await EventJournal.open(runtime.session, session_id)
            await journal.record(
                None,
                RuntimeEventType.SESSION_STARTED,
                SessionStartedPayload(workspace_identity=paths.identity),
            )
            await journal.record(
                "history", RuntimeEventType.USER_MESSAGE, UserMessagePayload(CONSTRAINT)
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
            clock.started = time.perf_counter()
            result = await run_headless(
                runtime.coordinator, PROMPT, provider="deepseek", model=model, session_id=session_id
            )
            report["total_ms"] = (time.perf_counter() - clock.started) * 1000
            report["first_text_ms"] = clock.milliseconds
            report["result"] = result.json_data()
            report["trace"] = [
                require_object(
                    normalize_json({**asdict(e), "timestamp": e.timestamp.isoformat()}), "trace"
                )
                for e in result.trace.entries
            ]
            events = [event async for event in runtime.session.read(session_id)]
            tools = [
                e.payload.result
                for e in events
                if e.turn_id == result.turn_id
                and isinstance(e.payload, ToolExecutionCompletedPayload)
            ]
            successful = [
                (index, r)
                for index, r in enumerate(tools)
                if r.status is ToolResultStatus.SUCCESS and not r.stop_turn
            ]
            names = {r.tool_name for _, r in successful}
            edit_index = max(
                (index for index, r in successful if r.tool_name == "edit_file"), default=-1
            )
            report["tools"] = [r.tool_name for r in tools]
            report["tool_results"] = [
                r.outcome.to_data() if r.outcome else {"output": r.output} for r in tools
            ]
            report["task_model_calls"] = result.trace.summary.model_calls
            report["tool_calls"] = result.trace.summary.tool_calls
            report["tool_execution_starts"] = sum(
                isinstance(e.payload, ToolExecutionStartedPayload)
                for e in events
                if e.turn_id == result.turn_id
            )
            report["compactions"] = sum(
                isinstance(e.payload, CompactionCommittedPayload) for e in events
            )
            checks = await asyncio.to_thread(_verify_files, workspace)
            checks.update(
                completed=result.stop_reason is StopReason.COMPLETED,
                required_tools={
                    "find_files",
                    "search_text",
                    "read_file",
                    "edit_file",
                    "run_command",
                }
                <= names,
                inspection_before_edit=all(
                    any(index < edit_index and r.tool_name == name for index, r in successful)
                    for name in ("find_files", "search_text", "read_file")
                ),
                verification_exit_zero=any(
                    r.tool_name == "run_command"
                    and r.outcome is not None
                    and r.outcome.data.get("exit_code") == 0
                    and not r.stop_turn
                    and index > edit_index >= 0
                    for index, r in successful
                ),
                compacted=report["compactions"] >= 1,
            )
            report["checks"] = checks
            report["status"] = (
                "passed"
                if all(
                    checks[name] is True
                    for name in (
                        "completed",
                        "required_tools",
                        "inspection_before_edit",
                        "verification_exit_zero",
                        "compacted",
                        "protected_unchanged",
                        "correct_sums",
                    )
                )
                else "failed"
            )
        finally:
            await runtime.aclose()
    except asyncio.CancelledError:
        report.update(status="cancelled", error_type="CancelledError")
        raise
    except Exception as error:
        report.update(status="error", error_type=type(error).__name__)
    finally:
        try:
            all_entries = await asyncio.to_thread(ledger.entries)
            entries = [
                e
                for e in all_entries
                if e.combination == sample.combination and e.attempt == sample.attempt
            ]
            report["requests"] = [
                require_object(normalize_json(asdict(e)), "entry") for e in entries
            ]
            report["calls_by_kind"] = dict(Counter(e.call_kind for e in entries))
            report["external_calls"] = len(entries)
            report["input_tokens"] = sum(e.input_tokens for e in entries)
            report["output_tokens"] = sum(e.output_tokens for e in entries)
            report["unknown_requests"] = sum(e.status != "settled" for e in entries)
            report["budget_exceeded"] = (
                len(all_entries) > ledger.limits.calls
                or sum(e.input_tokens for e in all_entries) > ledger.limits.input_tokens
                or sum(e.output_tokens for e in all_entries) > ledger.limits.output_tokens
            )
            if report["budget_exceeded"] or (
                report["status"] == "passed" and report["unknown_requests"]
            ):
                report["status"] = "failed"
        except Exception as error:
            report.update(status="error", accounting_error_type=type(error).__name__)
        await asyncio.to_thread(write_json, root / "result.json", report)
    return report
