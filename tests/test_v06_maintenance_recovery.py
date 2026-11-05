"""Restart paid maintenance stages and fence stale workers with actual model adapters."""

import asyncio
import json
import time
from pathlib import Path

import httpx
import pytest

from jdagent.domain.errors import StopReason
from jdagent.domain.events import (
    RuntimeEventType,
    SessionStartedPayload,
    TurnCompletedPayload,
    UserMessagePayload,
)
from jdagent.domain.model import Usage
from jdagent.eventing import EventJournal
from jdagent.memory_store import JobLease
from tests.runtime_matrix import COMBINATIONS, RuntimeCase, model_response


@pytest.mark.parametrize("combination", COMBINATIONS, ids=lambda pair: "/".join(pair))
@pytest.mark.parametrize("boundary", ["extraction", "merge", "lease_takeover"])
def test_reopened_maintenance_reuses_paid_stages_and_stale_lease_cannot_publish(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, combination: tuple[str, str], boundary: str
) -> None:
    case = RuntimeCase(tmp_path, monkeypatch, combination)
    source_event = ""
    requests: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        systems = "\n".join(m["content"] for m in body["messages"] if m["role"] == "system")
        if "Extract stable project facts only" in systems:
            requests.append("extract")
            text = json.dumps(
                {
                    "candidates": [
                        {
                            "statement": "project uses pytest",
                            "event_id": source_event,
                            "quote": "project uses pytest",
                        }
                    ]
                }
            )
        elif "Merge stable project memories" in systems:
            requests.append("merge")
            text = '{"selected":[0]}'
        else:
            requests.append("task")
            return model_response("done", usage=Usage(12, 4))
        assert "tools" not in body and body["response_format"] == {"type": "json_object"}
        return model_response(text, usage=Usage(12, 4))

    async def scenario() -> None:
        nonlocal source_event
        async with case.open(handler) as runtime:
            await runtime.session.acquire("source")
            journal = await EventJournal.open(runtime.session, "source")
            await journal.record(
                None,
                RuntimeEventType.SESSION_STARTED,
                SessionStartedPayload(workspace_identity=case.paths.identity),
            )
            source_event = (
                await journal.record(
                    "old", RuntimeEventType.USER_MESSAGE, UserMessagePayload("project uses pytest")
                )
            ).event_id
            await journal.record(
                "old",
                RuntimeEventType.TURN_COMPLETED,
                TurnCompletedPayload(StopReason.COMPLETED, 1, 0),
            )
            await runtime.session.release("source")
            turn = await runtime.coordinator.send("Start a new conversation.")
            assert (
                turn.result.stop_reason is StopReason.COMPLETED and turn.maintenance_job is not None
            )
            job = turn.maintenance_job
            worker = runtime.maintenance
            assert worker is not None
            worker.enabled = True
            store = worker.memory.store
            original = store.save_extraction
            fired = False
            stale: JobLease | None = None
            successor: JobLease | None = None

            def stop_after_save(lease: JobLease, session: str, digest: str, result: str) -> None:
                nonlocal fired, stale, successor
                original(lease, session, digest, result)
                target = session == "source" if boundary == "extraction" else session == "@merge"
                if target and not fired:
                    fired = True
                    stale = lease
                    if boundary == "lease_takeover":
                        successor = store.claim(
                            case.paths.identity, "successor", now=time.time() + 121
                        )
                        assert successor is not None and successor.epoch > lease.epoch
                    else:
                        raise asyncio.CancelledError

            monkeypatch.setattr(store, "save_extraction", stop_after_save)
            if boundary == "lease_takeover":
                await worker.run_pending()
                assert successor is not None and stale is not None
                assert store.job_status(job) == "running"
                with pytest.raises(ValueError, match="lease"):
                    store.finish(stale, candidates=(("stale worker must not publish", "invalid"),))
                assert store.recall(case.paths.identity, include_inactive=True) == ()
                store.finish(successor, error="controlled handoff", cancelled=True)
            else:
                with pytest.raises(asyncio.CancelledError):
                    await worker.run_pending()
                assert store.recall(case.paths.identity, include_inactive=True) == ()
            assert fired and store.job_status(job) == "pending"
            assert requests == (
                ["task", "extract"] if boundary == "extraction" else ["task", "extract", "merge"]
            )
            before_costs = store.costs(case.paths.identity)
            assert before_costs == (len(requests) * 12, len(requests) * 4)
        # Recreate all runtime and store objects, preserving only on-disk state.
        async with case.open(handler) as resumed:
            worker = resumed.maintenance
            assert worker is not None
            worker.enabled = True
            assert worker.memory.store.costs(case.paths.identity) == before_costs
            await worker.run_pending()
            assert worker.memory.store.job_status(job) == "succeeded"
            (memory,) = worker.memory.store.recall(case.paths.identity)
            assert memory.statement == "project uses pytest" and memory.version == 1
            assert worker.memory.store.costs(case.paths.identity) == (36, 12)
            await worker.run_pending()
            assert requests == ["task", "extract", "merge"]
            assert len(worker.memory.store.versions(memory.memory_id)) == 1
            assert (await worker.memory.search_memory("pytest"))["total"] == 1

    asyncio.run(scenario())
