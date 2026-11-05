"""Rehearse the exact comparison task through real adapters without network access."""

import asyncio
import json
from collections.abc import AsyncGenerator, Callable, Coroutine
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
import pytest

import jdagent.composition as composition_module
from jdagent.adapters.deepseek import DeepSeekModelPort
from jdagent.adapters.langchain_model import LangChainModelPort
from jdagent.domain.json import JsonObject, normalize_json, require_array, require_object
from jdagent.domain.model import Usage
from jdagent.domain.tools import ToolCall
from tests.experiment_budget import ExperimentLedger, ExperimentLimits
from tests.runtime_matrix import model_response
from tests.v06_comparison_task import EXECUTABLE, SAMPLES, Sample, run_fixed_task


class Script:
    def __init__(self, ledger: ExperimentLedger, *, omit_search: bool = False) -> None:
        self.ledger = ledger
        self.steps = 0
        self.omit_search = omit_search
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        entry = self.ledger.entries()[-1]
        assert entry.status == "reserved"
        body = require_object(normalize_json(json.loads(request.content)), "request")
        assert body["model"] == "deepseek-test"
        assert body["temperature"] == 0
        assert body["max_tokens"] == 2048
        assert request.extensions["timeout"]["read"] == 120
        if entry.call_kind == "compaction":
            return model_response(
                json.dumps(
                    {
                        "goal": "Fix sum_prices",
                        "facts": [],
                        "pending": [],
                        "decisions": [],
                        "paths": [],
                    }
                ),
                usage=Usage(12, 4),
            )
        assert entry.call_kind == "task"
        step = self.steps
        self.steps += 1
        args: JsonObject
        if step == 0:
            name = "find_files"
            args = {}
        elif step == 1:
            name = "find_files" if self.omit_search else "search_text"
            args = {} if self.omit_search else {"query": "sum_prices"}
        elif step == 2:
            name = "read_file"
            args = {"path": "prices.py"}
        elif step == 3:
            messages = [
                require_object(m, "message") for m in require_array(body["messages"], "messages")
            ]
            read = next(m for m in messages if m.get("tool_call_id") == "call-2")
            assert isinstance(read["content"], str)
            result = require_object(normalize_json(json.loads(read["content"])), "result")
            revision = require_object(result["data"], "data")["revision"]
            name = "edit_file"
            args = {
                "path": "prices.py",
                "revision": revision,
                "edits": [{"old_text": "total -= price", "new_text": "total += price"}],
            }
        elif step == 4:
            name = "run_command"
            args = {"program": EXECUTABLE, "args": ["verify.py"]}
        else:
            assert step == 5
            return model_response("Fixed and verified.", usage=Usage(12, 4))
        return model_response("", (ToolCall(f"call-{step}", name, args),), usage=Usage(12, 4))


@asynccontextmanager
async def adapter_http(
    monkeypatch: pytest.MonkeyPatch,
    sample: Sample,
    handler: Callable[[httpx.Request], httpx.Response]
    | Callable[[httpx.Request], Coroutine[None, None, httpx.Response]],
) -> AsyncGenerator[None]:
    adapter_type = LangChainModelPort if sample.adapter == "langchain" else DeepSeekModelPort
    async with httpx.AsyncClient(
        base_url="https://test", transport=httpx.MockTransport(handler)
    ) as client:

        def adapter(api_key: str, *, base_url: str) -> DeepSeekModelPort | LangChainModelPort:
            assert api_key == "dummy-key"
            return adapter_type(api_key, base_url=base_url, client=client)

        monkeypatch.setattr(composition_module, adapter_type.__name__, adapter)
        yield
        assert not client.is_closed


def test_six_samples_execute_fixed_task_with_one_durable_budget(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    limits = ExperimentLimits(96, 1200000, 120000)
    ledger = ExperimentLedger(tmp_path / "budget.sqlite", "offline-six", limits)

    async def scenario() -> None:
        for sample in SAMPLES:
            root = tmp_path / sample.label
            root.mkdir()
            script = Script(ledger)
            async with adapter_http(monkeypatch, sample, script):
                result = await run_fixed_task(
                    root,
                    sample,
                    ledger,
                    model="deepseek-test",
                    base_url="https://test",
                    api_key="dummy-key",
                )
            assert result["status"] == "passed", result
            assert result["compactions"] == 1
            assert result["tool_calls"] == result["tool_execution_starts"] == 5
            assert result["external_calls"] == len(script.requests) == 9
            assert result["calls_by_kind"] == {"task": 6, "compaction": 3}
            assert result["input_tokens"] == 108 and result["output_tokens"] == 36
            assert result["unknown_requests"] == 0
            assert result["first_text_ms"] is not None and result["total_ms"] is not None
            assert json.loads((root / "result.json").read_text(encoding="utf-8")) == result
            assert "dummy-key" not in (root / "result.json").read_text(encoding="utf-8")
        reopened = ExperimentLedger(ledger.path, ledger.experiment_id, limits)
        assert len(reopened.entries()) == 54
        assert sum(e.input_tokens for e in reopened.entries()) == 648

    asyncio.run(scenario())


@pytest.mark.parametrize("sample", [SAMPLES[0], SAMPLES[3]], ids=lambda s: s.label)
@pytest.mark.parametrize("failure", ["budget", "http"])
def test_failed_sample_retains_result_and_unknown_reservations(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    sample: Sample,
    failure: str,
) -> None:
    ledger = ExperimentLedger(
        tmp_path / "budget.sqlite",
        "failure",
        ExperimentLimits(10, 1 if failure == "budget" else 500000, 50000),
    )
    root = tmp_path / sample.label
    root.mkdir()
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(503, json={"error": {"message": "local simulated failure"}})

    async def scenario() -> None:
        async with adapter_http(monkeypatch, sample, handler):
            result = await run_fixed_task(
                root,
                sample,
                ledger,
                model="deepseek-test",
                base_url="https://test",
                api_key="dummy-key",
            )
        assert result["status"] == "failed", result
        assert (root / "result.json").exists()
        assert len(requests) == len(ledger.entries())  # No uncounted SDK attempts.
        if failure == "budget":
            assert not requests
        else:
            assert requests
            assert all(e.status == "unknown" for e in ledger.entries())
            reopened = ExperimentLedger(ledger.path, ledger.experiment_id, ledger.limits)
            assert reopened.entries() == ledger.entries()

    asyncio.run(scenario())


@pytest.mark.parametrize("sample", [SAMPLES[0], SAMPLES[3]], ids=lambda s: s.label)
def test_cancelled_sample_keeps_evidence_and_reserved_cost(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    sample: Sample,
) -> None:
    ledger = ExperimentLedger(
        tmp_path / "budget.sqlite", "cancel", ExperimentLimits(10, 500000, 50000)
    )
    root = tmp_path / sample.label
    root.mkdir()

    async def scenario() -> None:
        started = asyncio.Event()

        async def handler(request: httpx.Request) -> httpx.Response:
            del request
            started.set()
            await asyncio.Event().wait()
            raise AssertionError("unreachable")

        async with adapter_http(monkeypatch, sample, handler):
            operation = asyncio.create_task(
                run_fixed_task(
                    root,
                    sample,
                    ledger,
                    model="deepseek-test",
                    base_url="https://test",
                    api_key="dummy-key",
                )
            )
            await asyncio.wait_for(started.wait(), 3)
            operation.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(operation, 3)
        report = json.loads((root / "result.json").read_text(encoding="utf-8"))
        assert report["status"] == "cancelled"
        assert report["unknown_requests"] == 1
        assert len(ledger.entries()) == 1
        assert ledger.entries()[0].status == "unknown"

    asyncio.run(scenario())


@pytest.mark.parametrize("sample", [SAMPLES[0], SAMPLES[3]], ids=lambda s: s.label)
def test_correct_patch_without_required_search_is_not_a_passing_sample(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    sample: Sample,
) -> None:
    ledger = ExperimentLedger(
        tmp_path / "budget.sqlite", "missing-step", ExperimentLimits(20, 500000, 50000)
    )
    root = tmp_path / sample.label
    root.mkdir()
    script = Script(ledger, omit_search=True)

    async def scenario() -> None:
        async with adapter_http(monkeypatch, sample, script):
            result = await run_fixed_task(
                root,
                sample,
                ledger,
                model="deepseek-test",
                base_url="https://test",
                api_key="dummy-key",
            )
        checks = require_object(result["checks"], "checks")
        assert checks["correct_sums"] and checks["verification_exit_zero"]
        assert not checks["required_tools"]
        assert result["status"] == "failed"

    asyncio.run(scenario())
