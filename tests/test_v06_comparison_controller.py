"""Validate comparison admission, frozen criteria, failure retention and recovery."""

import asyncio
import hashlib
import json
import sqlite3
import sys
import threading
import time
from pathlib import Path

import httpx
import pytest

import tests.v06_comparison as comparison
from jdagent.adapters.session_lock import SessionLease
from jdagent.domain.errors import SessionError
from jdagent.domain.json import JsonObject, normalize_json, require_object
from jdagent.domain.model import MessageRole, ModelMessage, ModelRequest, ModelSettings
from tests.experiment_budget import ExperimentLedger, ExperimentLimits
from tests.test_v06_comparison_task import Script, adapter_http
from tests.v06_comparison_task import SAMPLES, write_json


def _read(path: Path) -> JsonObject:
    return require_object(normalize_json(json.loads(path.read_text(encoding="utf-8"))), "record")


def _ledger(root: Path) -> ExperimentLedger:
    return ExperimentLedger(
        root / "budget.sqlite", "offline-controller", ExperimentLimits(96, 1200000, 120000)
    )


@pytest.fixture
def experiment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(
        comparison,
        "repository_snapshot",
        lambda: {
            "revision": "offline-revision",
            "lock_sha256": "offline-lock",
            "dependency_bytes": 1234,
        },
    )
    root = tmp_path / "experiment"
    comparison.prepare(
        root,
        experiment_id="offline-controller",
        model="deepseek-test",
        base_url="https://test",
        limits=ExperimentLimits(96, 1200000, 120000),
    )
    return root


def criteria() -> JsonObject:
    return {
        name: {"maximum": 1e9, "basis": "Offline admission test; not a live acceptance ceiling"}
        for name in comparison.METRICS
    }


@pytest.mark.parametrize("reject_target", [False, True])
def test_controller_runs_ordered_samples_and_enforces_frozen_ceilings(
    experiment: Path,
    monkeypatch: pytest.MonkeyPatch,
    reject_target: bool,
) -> None:
    # Match the documented CLI invocation: a relative experiment directory,
    # with commands running in a different workspace than the launcher cwd.
    monkeypatch.chdir(experiment.parent)
    experiment = Path(experiment.name)
    ledger = _ledger(experiment)

    async def scenario() -> None:
        with pytest.raises(FileNotFoundError):
            await comparison.execute(experiment, "B2", api_key="dummy-key", authorized=True)
        with pytest.raises(FileNotFoundError):
            comparison.freeze_criteria(experiment, criteria())
        for sample in SAMPLES:
            if sample.label == "T1":
                for invalid in (None, -1, True, float("nan")):
                    bad = criteria()
                    bad["total_ms"] = {"maximum": invalid, "basis": "invalid test ceiling"}
                    with pytest.raises(ValueError, match="finite nonnegative"):
                        comparison.freeze_criteria(experiment, bad)
                    assert not (experiment / "criteria.json").exists()
                selected = criteria()
                if reject_target:
                    selected["total_ms"] = {
                        "maximum": 0,
                        "basis": "Force an offline comparison failure",
                    }
                comparison.freeze_criteria(experiment, selected)
                with pytest.raises(FileExistsError):
                    comparison.freeze_criteria(experiment, selected)
                frozen_path = experiment / "criteria.json"
                original = frozen_path.read_bytes()
                edited = _read(frozen_path)
                require_object(edited["metrics"], "metrics")["total_ms"] = {
                    "maximum": 1e10,
                    "basis": "edited after freeze, before T1",
                }
                frozen_path.write_text(json.dumps(edited), encoding="utf-8")
                with pytest.raises(ValueError, match="changed"):
                    await comparison.execute(experiment, "T1", api_key="dummy-key", authorized=True)
                assert not (experiment / "T1").exists()
                frozen_path.write_bytes(original)
                with pytest.raises(ValueError, match="startup measurement scope"):
                    await comparison.execute(
                        experiment,
                        "T1",
                        api_key="dummy-key",
                        authorized=True,
                        process_started=time.perf_counter(),
                    )
            script = Script(ledger)
            async with adapter_http(monkeypatch, sample, script):
                report = await comparison.execute(
                    experiment, sample.label, api_key="dummy-key", authorized=True
                )
            expected = "failed" if reject_target and sample.engine == "langgraph" else "passed"
            assert report["status"] == expected, report
            if expected == "failed":
                assert report["exceeded"] == ["total_ms"]
                assert _read(experiment / sample.label / "result.json")["status"] == "passed"
            with pytest.raises(FileExistsError):
                await comparison.execute(
                    experiment, sample.label, api_key="dummy-key", authorized=True
                )
            assert len(script.requests) == 9
        assert len(ledger.entries()) == 54
        frozen = experiment / "criteria.json"
        changed = _read(frozen)
        require_object(changed["metrics"], "metrics")["total_ms"] = {
            "maximum": 1e10,
            "basis": "too late",
        }
        frozen.write_text(json.dumps(changed), encoding="utf-8")
        with pytest.raises(ValueError, match="changed"):
            await comparison.execute(experiment, "T1", api_key="dummy-key", authorized=True)
        assert len(ledger.entries()) == 54

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "change", ["plan", "environment", "fixture", "missing_ledger", "corrupt_ledger"]
)
def test_input_drift_blocks_before_claim_or_model_construction(
    experiment: Path,
    monkeypatch: pytest.MonkeyPatch,
    change: str,
) -> None:
    if change == "plan":
        with (experiment / "plan.json").open("a", encoding="utf-8") as stream:
            stream.write(" ")
    elif change == "environment":
        monkeypatch.setattr(comparison, "repository_snapshot", lambda: {"revision": "changed"})
    elif change == "fixture":
        monkeypatch.setattr(comparison, "fixture_fingerprint", lambda: {"changed": True})
    elif change == "missing_ledger":
        (experiment / "budget.sqlite").unlink()
    else:
        (experiment / "budget.sqlite").write_bytes(b"corrupt")

    async def forbidden(*args: object, **kwargs: object) -> JsonObject:
        del args, kwargs
        raise AssertionError("Must reject before calling the task executor")

    monkeypatch.setattr(comparison, "run_fixed_task", forbidden)
    with pytest.raises((ValueError, sqlite3.DatabaseError)):
        asyncio.run(comparison.execute(experiment, "B1", api_key="dummy-key", authorized=True))
    assert not (experiment / "B1").exists()


def test_no_opt_in_or_real_key_at_offline_endpoint_never_claims_a_sample(experiment: Path) -> None:
    with pytest.raises(ValueError, match="opt-in"):
        asyncio.run(comparison.execute(experiment, "B1", api_key="dummy-key"))
    with pytest.raises(ValueError, match="synthetic test key"):
        asyncio.run(comparison.execute(experiment, "B1", api_key="not-a-real-key", authorized=True))
    assert not (experiment / "B1").exists()


def test_interrupted_claim_is_consumed_without_resetting_budget(experiment: Path) -> None:
    directory = experiment / "B1"
    directory.mkdir()
    write_json(
        directory / "claim.json",
        {
            "sample": "B1",
            "plan_sha256": hashlib.sha256((experiment / "plan.json").read_bytes()).hexdigest(),
            "criteria_sha256": None,
        },
    )
    ledger = _ledger(experiment)
    ledger.reserve(
        ModelRequest(
            model="deepseek-test",
            settings=ModelSettings(max_output_tokens=64),
            messages=(ModelMessage(MessageRole.USER, "synthetic interrupted request"),),
            metadata={"request_id": "interrupted", "call_kind": "task"},
        ),
        "native/native",
        1,
    )
    before = ledger.entries()
    lease = SessionLease(experiment / "controller.lock")
    try:
        with pytest.raises(SessionError):
            comparison.record_interruption(experiment, "B1")
        with pytest.raises(SessionError):
            asyncio.run(comparison.execute(experiment, "B1", api_key="dummy-key", authorized=True))
    finally:
        lease.close()
    comparison.record_interruption(experiment, "B1")
    assert _read(experiment / "B1" / "completion.json")["status"] == "interrupted"
    assert ledger.entries() == before
    with pytest.raises(FileExistsError):
        asyncio.run(comparison.execute(experiment, "B1", api_key="dummy-key", authorized=True))
    with pytest.raises(FileExistsError):
        comparison.record_interruption(experiment, "B1")
    with pytest.raises(ValueError, match="baselines must pass"):
        comparison.freeze_criteria(experiment, criteria())


def test_result_tampering_is_detected_before_next_attempt(
    experiment: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ledger = _ledger(experiment)

    async def scenario() -> None:
        async with adapter_http(monkeypatch, SAMPLES[0], Script(ledger)):
            await comparison.execute(experiment, "B1", api_key="dummy-key", authorized=True)
        path = experiment / "B1" / "result.json"
        path.write_text("{}", encoding="utf-8")
        with pytest.raises(ValueError, match="result changed"):
            await comparison.execute(experiment, "B2", api_key="dummy-key", authorized=True)
        assert len(ledger.entries()) == 9
        assert not (experiment / "B2").exists()

    asyncio.run(scenario())


def test_completed_measurement_edit_is_rejected_before_next_attempt(
    experiment: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ledger = _ledger(experiment)

    async def forbidden(*args: object, **kwargs: object) -> JsonObject:
        del args, kwargs
        raise AssertionError("Edited measurement must be rejected before task execution")

    async def scenario() -> None:
        async with adapter_http(monkeypatch, SAMPLES[0], Script(ledger)):
            await comparison.execute(experiment, "B1", api_key="dummy-key", authorized=True)
        path = experiment / "B1" / "completion.json"
        completion = _read(path)
        require_object(completion["metrics"], "metrics")["total_ms"] = 0
        path.write_text(json.dumps(completion), encoding="utf-8")
        monkeypatch.setattr(comparison, "run_fixed_task", forbidden)
        with pytest.raises(ValueError, match="changed"):
            await comparison.execute(experiment, "B2", api_key="dummy-key", authorized=True)
        assert not (experiment / "B2").exists()

    asyncio.run(scenario())


@pytest.mark.parametrize("flag, environment", [(False, "1"), (True, "0")])
def test_cli_rejects_missing_opt_in_before_reading_any_key(
    experiment: Path,
    monkeypatch: pytest.MonkeyPatch,
    flag: bool,
    environment: str,
) -> None:
    def forbidden_key(*args: object) -> str:
        del args
        raise AssertionError("Credentials must remain unread")

    monkeypatch.setattr(comparison, "load_deepseek_api_key", forbidden_key)
    monkeypatch.setenv("JDAGENT_RUN_V06_LIVE", environment)
    monkeypatch.setattr(
        sys,
        "argv",
        ["comparison", "run", str(experiment), "B1", *(["--allow-live"] if flag else [])],
    )
    with pytest.raises(ValueError, match="only after authorization"):
        comparison.main()


def test_cancellation_during_os_lock_acquisition_does_not_leak_ownership(
    experiment: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started, release = threading.Event(), threading.Event()

    def delayed_lease(path: Path) -> SessionLease:
        started.set()
        assert release.wait(3)
        return SessionLease(path)

    monkeypatch.setattr(comparison, "SessionLease", delayed_lease)

    async def scenario() -> None:
        operation = asyncio.create_task(
            comparison.execute(experiment, "B1", api_key="dummy-key", authorized=True)
        )
        assert await asyncio.to_thread(started.wait, 3)
        operation.cancel()
        await asyncio.sleep(0)
        operation.cancel()
        await asyncio.sleep(0)
        assert not operation.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(operation, 3)
        lease = SessionLease(experiment / "controller.lock")
        lease.close()
        assert not (experiment / "B1").exists()

    try:
        asyncio.run(scenario())
    finally:
        release.set()


def test_controller_records_cancelled_sample_and_releases_experiment_lock(
    experiment: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        started = asyncio.Event()

        async def handler(request: httpx.Request) -> httpx.Response:
            del request
            started.set()
            await asyncio.Event().wait()
            raise AssertionError("unreachable")

        async with adapter_http(monkeypatch, SAMPLES[0], handler):
            operation = asyncio.create_task(
                comparison.execute(experiment, "B1", api_key="dummy-key", authorized=True)
            )
            await asyncio.wait_for(started.wait(), 3)
            operation.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(operation, 3)
        assert _read(experiment / "B1" / "completion.json")["status"] == "cancelled"
        assert _ledger(experiment).entries()[0].status == "unknown"
        lease = SessionLease(experiment / "controller.lock")
        lease.close()
        with pytest.raises(FileExistsError):
            await comparison.execute(experiment, "B1", api_key="dummy-key", authorized=True)

    asyncio.run(scenario())
