"""Opt-in fsync-preserving scale evidence, separate from the fast default suite."""

import asyncio
import json
import os
import statistics
import time
from pathlib import Path

import pytest

from jdagent.adapters.jsonl_session import JsonlSession
from jdagent.domain.events import RuntimeEvent, RuntimeEventType, UserMessagePayload


@pytest.mark.parametrize("count", [1000, 10000, 100000])
def test_owned_append_and_reopen_scale(tmp_path: Path, count: int):
    if os.environ.get("JDAGENT_RUN_STORAGE_SCALE") != "1":
        pytest.skip("Set JDAGENT_RUN_STORAGE_SCALE=1 for durable 1k/10k/100k storage measurements")

    async def scenario():
        session = JsonlSession(tmp_path)
        await session.acquire("scale")
        latencies: list[float] = []
        started = time.perf_counter()
        for index in range(1, count + 1):
            event = RuntimeEvent.create(
                session_id="scale",
                turn_id="t",
                sequence=index,
                event_type=RuntimeEventType.USER_MESSAGE,
                payload=UserMessagePayload("small durable original"),
            )
            tick = time.perf_counter()
            await session.append(event)
            latencies.append(time.perf_counter() - tick)
        elapsed = time.perf_counter() - started
        await session.release("scale")
        reopen: list[float] = []
        for _ in range(3):
            tick = time.perf_counter()
            restored = [event async for event in JsonlSession(tmp_path).read("scale")]
            reopen.append(time.perf_counter() - tick)
            assert len(restored) == count and restored[-1].sequence == count
        report = {
            "events": count,
            "append_seconds": elapsed,
            "append_p50_ms": statistics.median(latencies) * 1000,
            "append_p95_ms": sorted(latencies)[int(count * 0.95)] * 1000,
            "reopen_seconds": reopen,
            "bytes": session.path_for("scale").stat().st_size,
        }
        (tmp_path / "scale.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(json.dumps(report))

    asyncio.run(scenario())
