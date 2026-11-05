"""Opt-in controller for six ordered, immutable v0.6 comparison attempts."""

# The timestamp must precede runtime imports to measure their startup cost.
# ruff: noqa: E402

import argparse
import asyncio
import hashlib
import importlib.metadata
import json
import math
import os
import platform
import subprocess
import sys
import time
from pathlib import Path

ENTRY_TIME = time.perf_counter()

from jdagent.adapters.session_lock import SessionLease
from jdagent.composition import load_deepseek_api_key
from jdagent.domain.json import JsonObject, normalize_json, require_object, require_string
from tests.experiment_budget import ExperimentLedger, ExperimentLimits
from tests.v06_comparison_task import (
    SAMPLES,
    Sample,
    fixture_fingerprint,
    run_fixed_task,
    write_json,
)

REPOSITORY = Path(__file__).resolve().parents[1]
METRICS = (
    "first_text_ms",
    "total_ms",
    "startup_ms",
    "external_calls",
    "input_tokens",
    "output_tokens",
    "tool_calls",
    "tool_execution_starts",
    "dependency_bytes",
)


def _hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _read(path: Path) -> JsonObject:
    return require_object(normalize_json(json.loads(path.read_text(encoding="utf-8"))), str(path))


def _number(value: object, name: str) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (float, int))
        or not math.isfinite(value)
        or value < 0
    ):
        raise ValueError(f"{name} must be a finite nonnegative number")
    return value


def repository_snapshot() -> JsonObject:
    """Refuse uncommitted inputs and record the installed environment, without keys."""

    def git(*arguments: str) -> str:
        return subprocess.run(
            ["git", "-c", f"safe.directory={REPOSITORY.as_posix()}", *arguments],
            cwd=REPOSITORY,
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
        ).stdout.strip()

    if git("status", "--porcelain"):
        raise ValueError("Comparison requires a clean committed working tree")
    installed: JsonObject = {}
    files: set[Path] = set()
    for distribution in importlib.metadata.distributions():
        installed[distribution.metadata["Name"]] = distribution.version
        for item in distribution.files or ():
            if "__pycache__" not in item.parts and item.suffix != ".pyc":
                path = Path(str(distribution.locate_file(item))).resolve()
                if path.is_file():
                    files.add(path)
    return {
        "revision": git("rev-parse", "HEAD"),
        "lock_sha256": _hash(REPOSITORY / "uv.lock"),
        "python": platform.python_version(),
        "platform": platform.platform(),
        "executable": str(Path(sys.executable).resolve()),
        "packages": installed,
        "dependency_bytes": sum(p.stat().st_size for p in files),
    }


def prepare(
    root: Path, *, experiment_id: str, model: str, base_url: str, limits: ExperimentLimits
) -> None:
    if not experiment_id.strip() or not model.strip():
        raise ValueError("Experiment ID and explicit model are required")
    if base_url not in {"https://api.deepseek.com", "https://api.deepseek.com/v1", "https://test"}:
        raise ValueError("Use the fixed DeepSeek endpoint (or the offline test endpoint)")
    snapshot = repository_snapshot()
    inputs = fixture_fingerprint()
    root = root.resolve()
    root.mkdir(parents=True, exist_ok=False)
    plan: JsonObject = {
        "schema_version": 1,
        "experiment_id": experiment_id,
        "directory": str(root),
        "model": model,
        "base_url": base_url,
        "environment": snapshot,
        "fixture": inputs,
        "limits": {
            "calls": limits.calls,
            "input_tokens": limits.input_tokens,
            "output_tokens": limits.output_tokens,
        },
        "samples": [s.label for s in SAMPLES],
    }
    write_json(root / "plan.json", plan)
    (root / "plan.sha256").write_text(_hash(root / "plan.json"), encoding="ascii")
    ExperimentLedger(root / "budget.sqlite", experiment_id, limits)


def _plan(root: Path, *, current: bool = True) -> JsonObject:
    if _hash(root / "plan.json") != (root / "plan.sha256").read_text(encoding="ascii"):
        raise ValueError("Prepared experiment plan changed")
    plan = _read(root / "plan.json")
    if plan.get("directory") != str(root.resolve()) or plan.get("schema_version") != 1:
        raise ValueError("Experiment directory or schema changed")
    if current and (
        plan["environment"] != repository_snapshot() or plan["fixture"] != fixture_fingerprint()
    ):
        raise ValueError("Code, dependencies, environment, or fixture changed; no request allowed")
    return plan


def _ledger(root: Path, plan: JsonObject) -> ExperimentLedger:
    if not (root / "budget.sqlite").is_file():
        raise ValueError("Prepared budget ledger is missing; it must not be recreated")
    values = require_object(plan["limits"], "limits")
    counts: list[int] = []
    for name in ("calls", "input_tokens", "output_tokens"):
        value = values[name]
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError("Invalid fixed budget")
        counts.append(value)
    return ExperimentLedger(
        root / "budget.sqlite", require_string(plan, "experiment_id"), ExperimentLimits(*counts)
    )


def _sample(label: str) -> Sample:
    for sample in SAMPLES:
        if sample.label == label:
            return sample
    raise ValueError("Sample must be B1/B2/B3/T1/T2/T3")


def _completed(root: Path, sample: Sample) -> JsonObject:
    directory = root / sample.label
    completion = _read(directory / "completion.json")
    claim = _read(directory / "claim.json")
    if claim.get("plan_sha256") != _hash(root / "plan.json") or claim.get("sample") != sample.label:
        raise ValueError("Sample claim does not match prepared plan")
    if completion.get("claim_sha256") != _hash(directory / "claim.json"):
        raise ValueError("Sample claim changed after completion")
    if completion.get("result_sha256") is not None and completion["result_sha256"] != _hash(
        directory / "result.json"
    ):
        raise ValueError("Sample result changed after completion")
    return completion


def _baseline_hashes(root: Path) -> JsonObject:
    hashes: JsonObject = {}
    for sample in SAMPLES[:3]:
        completion = _completed(root, sample)
        if completion.get("status") != "passed":
            raise ValueError("All three native baselines must pass before targets")
        metrics = require_object(completion["metrics"], "baseline metrics")
        for metric in METRICS:
            _number(metrics.get(metric), metric)
        hashes[sample.label] = _hash(root / sample.label / "completion.json")
    return hashes


def freeze_criteria(root: Path, criteria: JsonObject) -> None:
    lease = SessionLease(root / "controller.lock")
    try:
        _plan(root)
        if any((root / s.label).exists() for s in SAMPLES[3:]):
            raise ValueError("Criteria cannot change after a target was claimed")
        baseline = _baseline_hashes(root)
        scopes = {require_string(_completed(root, s), "startup_scope") for s in SAMPLES[:3]}
        if len(scopes) != 1:
            raise ValueError("Baselines used different startup measurement scopes")
        if set(criteria) != set(METRICS):
            raise ValueError("Criteria must provide every comparison metric")
        for name in METRICS:
            item = require_object(criteria[name], name)
            _number(item.get("maximum"), name)
            if not require_string(item, "basis").strip():
                raise ValueError("Every ceiling requires a baseline-based explanation")
        write_json(
            root / "criteria.json",
            {
                "plan_sha256": _hash(root / "plan.json"),
                "baseline": baseline,
                "metrics": criteria,
                "startup_scope": scopes.pop(),
            },
        )
    finally:
        lease.close()


def _criteria(root: Path, startup_scope: str) -> JsonObject:
    criteria = _read(root / "criteria.json")
    if criteria.get("plan_sha256") != _hash(root / "plan.json") or criteria.get(
        "baseline"
    ) != _baseline_hashes(root):
        raise ValueError("Frozen criteria no longer match the plan and baselines")
    if criteria.get("startup_scope") != startup_scope:
        raise ValueError("Target startup measurement scope differs from baselines")
    for sample in SAMPLES[3:]:
        claim = root / sample.label / "claim.json"
        if claim.exists() and _read(claim).get("criteria_sha256") != _hash(root / "criteria.json"):
            raise ValueError("Criteria changed after target admission")
    return require_object(criteria["metrics"], "metrics")


async def _acquire_lease(path: Path) -> SessionLease:
    acquisition = asyncio.create_task(asyncio.to_thread(SessionLease, path))
    try:
        return await asyncio.shield(acquisition)
    except asyncio.CancelledError:
        # Cancelling to_thread does not stop the OS open. Keep ownership of its
        # result until it can be closed, including repeated caller cancellation.
        while not acquisition.done():
            try:
                await asyncio.shield(acquisition)
            except asyncio.CancelledError:
                continue
            except Exception:
                break
        try:
            lease = acquisition.result()
        except Exception:
            pass
        else:
            lease.close()
        raise


async def execute(
    root: Path,
    label: str,
    *,
    api_key: str,
    authorized: bool = False,
    process_started: float | None = None,
) -> JsonObject:
    if not authorized:
        raise ValueError("Explicit live opt-in is required before opening a sample")
    if not api_key.strip():
        raise ValueError("An explicit credential is required")
    started = process_started if process_started is not None else time.perf_counter()
    startup_scope = (
        "runtime_imports_to_runtime_ready"
        if process_started is not None
        else "in_process_invocation_to_runtime_ready"
    )
    lease = await _acquire_lease(root / "controller.lock")
    try:
        plan = await asyncio.to_thread(_plan, root)
        if plan["base_url"] == "https://test" and api_key != "dummy-key":
            raise ValueError("Offline endpoint accepts only the synthetic test key")
        ledger = await asyncio.to_thread(_ledger, root, plan)
        sample = _sample(label)
        for preceding in SAMPLES[: SAMPLES.index(sample)]:
            await asyncio.to_thread(_completed, root, preceding)
        criteria = (
            await asyncio.to_thread(_criteria, root, startup_scope)
            if sample.engine == "langgraph"
            else None
        )
        directory = root / sample.label
        await asyncio.to_thread(
            directory.mkdir
        )  # Existing/crashed/failed attempts are never reused.
        claim: JsonObject = {
            "sample": label,
            "plan_sha256": _hash(root / "plan.json"),
            "criteria_sha256": _hash(root / "criteria.json") if criteria is not None else None,
        }
        await asyncio.to_thread(write_json, directory / "claim.json", claim)
        before_task = time.perf_counter()
        try:
            await run_fixed_task(
                directory,
                sample,
                ledger,
                model=require_string(plan, "model"),
                base_url=require_string(plan, "base_url"),
                api_key=api_key,
            )
        finally:
            if (directory / "result.json").exists():
                result = await asyncio.to_thread(_read, directory / "result.json")
                environment = require_object(plan["environment"], "environment")
                metrics: JsonObject = {name: result.get(name) for name in METRICS}
                setup = result.get("setup_ms")
                metrics["startup_ms"] = (
                    (before_task - started) * 1000 + float(setup)
                    if isinstance(setup, (int, float))
                    else None
                )
                metrics["dependency_bytes"] = environment["dependency_bytes"]
                exceeded: list[str] = []
                if criteria is not None:
                    for name in METRICS:
                        ceiling = require_object(criteria[name], name)
                        value = metrics[name]
                        if value is None or _number(value, name) > _number(
                            ceiling["maximum"], name
                        ):
                            exceeded.append(name)
                completion: JsonObject = {
                    "sample": label,
                    "claim_sha256": _hash(directory / "claim.json"),
                    "result_sha256": _hash(directory / "result.json"),
                    "metrics": metrics,
                    "status": "failed"
                    if exceeded and result["status"] == "passed"
                    else result["status"],
                    "exceeded": normalize_json(exceeded),
                    "startup_scope": startup_scope,
                }
                await asyncio.to_thread(write_json, directory / "completion.json", completion)
        return await asyncio.to_thread(_completed, root, sample)
    finally:
        lease.close()


def record_interruption(root: Path, label: str) -> None:
    """Consume a stranded claim only while the OS lock proves no controller owns it."""
    lease = SessionLease(root / "controller.lock")
    try:
        plan = _plan(root, current=False)
        ledger = _ledger(root, plan)
        sample = _sample(label)
        directory = root / sample.label
        claim = _read(directory / "claim.json")
        if claim.get("plan_sha256") != _hash(root / "plan.json"):
            raise ValueError("Interrupted claim does not match plan")
        entries = [
            e
            for e in ledger.entries()
            if e.combination == sample.combination and e.attempt == sample.attempt
        ]
        write_json(
            directory / "completion.json",
            {
                "sample": label,
                "status": "interrupted",
                "claim_sha256": _hash(directory / "claim.json"),
                "result_sha256": _hash(directory / "result.json")
                if (directory / "result.json").exists()
                else None,
                "external_calls": len(entries),
                "input_tokens": sum(e.input_tokens for e in entries),
                "output_tokens": sum(e.output_tokens for e in entries),
                "metrics": {},
            },
        )
    finally:
        lease.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    prepare_parser = commands.add_parser("prepare")
    prepare_parser.add_argument("root", type=Path)
    prepare_parser.add_argument("--experiment-id", required=True)
    prepare_parser.add_argument("--model", required=True)
    prepare_parser.add_argument("--base-url", default="https://api.deepseek.com")
    prepare_parser.add_argument("--calls", type=int, required=True)
    prepare_parser.add_argument("--input-tokens", type=int, required=True)
    prepare_parser.add_argument("--output-tokens", type=int, required=True)
    run_parser = commands.add_parser("run")
    run_parser.add_argument("root", type=Path)
    run_parser.add_argument("sample", choices=[s.label for s in SAMPLES])
    run_parser.add_argument("--allow-live", action="store_true")
    freeze_parser = commands.add_parser("freeze-criteria")
    freeze_parser.add_argument("root", type=Path)
    freeze_parser.add_argument("criteria", type=Path)
    recover_parser = commands.add_parser("record-interruption")
    recover_parser.add_argument("root", type=Path)
    recover_parser.add_argument("sample", choices=[s.label for s in SAMPLES])
    args = parser.parse_args()
    if args.command == "prepare":
        prepare(
            args.root,
            experiment_id=args.experiment_id,
            model=args.model,
            base_url=args.base_url,
            limits=ExperimentLimits(args.calls, args.input_tokens, args.output_tokens),
        )
    elif args.command == "freeze-criteria":
        freeze_criteria(args.root, _read(args.criteria))
    elif args.command == "record-interruption":
        record_interruption(args.root, args.sample)
    else:
        if not args.allow_live or os.environ.get("JDAGENT_RUN_V06_LIVE") != "1":
            raise ValueError("Set JDAGENT_RUN_V06_LIVE=1 and --allow-live only after authorization")
        plan = _plan(args.root)  # Verify fixed inputs before discovering a credential.
        if plan["base_url"] == "https://test":
            raise ValueError("The offline endpoint is for injected test transports only")
        key = load_deepseek_api_key(os.environ.get("DEEPSEEK_API_KEY"))
        if key is None:
            raise ValueError("DeepSeek credential is unavailable")
        result = asyncio.run(
            execute(
                args.root, args.sample, api_key=key, authorized=True, process_started=ENTRY_TIME
            )
        )
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 0 if result["status"] == "passed" else 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
