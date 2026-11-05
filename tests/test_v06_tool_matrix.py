"""Verify observable tool effects and cancellation through every backend composition."""

import asyncio
import json
import runpy
import shutil
import sys
from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest

from jdagent.adapters.fake import FakeApproval
from jdagent.adapters.jsonl_session import JsonlSession
from jdagent.composition import RuntimeOptions
from jdagent.core.contracts import CancellationToken
from jdagent.domain.errors import StopReason
from jdagent.domain.events import (
    RuntimeEvent,
    ToolExecutionCompletedPayload,
    ToolExecutionStartedPayload,
    TurnCompletedPayload,
    TurnFailedPayload,
)
from jdagent.domain.json import JsonObject, normalize_json, require_array, require_object
from jdagent.domain.tools import ApprovalDecision, ToolCall, ToolErrorCode
from tests.runtime_matrix import COMBINATIONS, RuntimeCase, model_response


@pytest.fixture(params=COMBINATIONS, ids=lambda pair: "/".join(pair))
def case(
    request: pytest.FixtureRequest, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> RuntimeCase:
    return RuntimeCase(tmp_path, monkeypatch, request.param)


def test_read_edit_and_commands_have_ordered_real_effects(case: RuntimeCase) -> None:
    source = Path(__file__).resolve().parents[1] / "testdata" / "v0.4-tools"
    shutil.copytree(source, case.workspace)
    protected = (case.workspace / "verify.py").read_bytes()
    requests: list[JsonObject] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = require_object(normalize_json(json.loads(request.content)), "request")
        step = len(requests)
        requests.append(body)
        args: JsonObject
        steps: list[tuple[str, JsonObject]] = [
            ("find_files", {}),
            ("search_text", {"query": "sum_prices"}),
            ("read_file", {"path": "prices.py"}),
            ("run_command", {"program": sys.executable, "args": ["verify.py"]}),
        ]
        if step < 4:
            name, args = steps[step]
        elif step == 4:
            messages = [require_object(m, "message") for m in require_array(body["messages"], "m")]
            read = next(m for m in messages if m.get("tool_call_id") == "call-2")
            content = read["content"]
            assert isinstance(content, str)
            result = require_object(normalize_json(json.loads(content)), "result")
            revision = require_object(result["data"], "data")["revision"]
            name = "edit_file"
            args = {
                "path": "prices.py",
                "revision": revision,
                "edits": [{"old_text": "total -= price", "new_text": "total += price"}],
            }
        elif step == 5:
            name = "run_command"
            args = {"program": sys.executable, "args": ["verify.py"]}
        else:
            assert step == 6
            return model_response("Fixed and verified.")
        return model_response("", (ToolCall(f"call-{step}", name, args),))

    async def scenario() -> None:
        async with case.open(handler) as runtime:
            turn = await runtime.coordinator.send("Find, repair and verify the price sum bug.")
            assert turn.result.stop_reason is StopReason.COMPLETED
            assert (turn.result.model_calls, turn.result.tool_calls) == (7, 6)
            events = [event async for event in runtime.session.read(turn.session_id)]
            results = [
                e.payload.result
                for e in events
                if isinstance(e.payload, ToolExecutionCompletedPayload)
            ]
            assert [r.tool_name for r in results] == [
                "find_files",
                "search_text",
                "read_file",
                "run_command",
                "edit_file",
                "run_command",
            ]
            failed, succeeded = results[3].outcome, results[5].outcome
            assert failed is not None and failed.observed_failure
            assert succeeded is not None and succeeded.data["exit_code"] == 0
            assert sum(isinstance(e.payload, ToolExecutionStartedPayload) for e in events) == 6
        assert (case.workspace / "verify.py").read_bytes() == protected
        module = runpy.run_path(str(case.workspace / "prices.py"))
        assert module["sum_prices"]([2, 3, 7]) == 12
        assert module["sum_prices"]([]) == 0

    asyncio.run(scenario())


def test_repeated_rejected_write_does_not_reprompt_or_create_file(case: RuntimeCase) -> None:
    approval = FakeApproval(ApprovalDecision.REJECT)
    count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal count
        del request
        count += 1
        if count <= 2:
            return model_response(
                "", (ToolCall(f"deny-{count}", "create_file", {"path": "a.txt", "content": "a"}),)
            )
        return model_response("Write was denied.")

    async def scenario() -> None:
        async with case.open(handler, approval=approval) as runtime:
            turn = await runtime.coordinator.send("Create a file.")
            assert turn.result.stop_reason is StopReason.COMPLETED
            assert turn.result.tool_calls == 2
            events = [e async for e in runtime.session.read(turn.session_id)]
            results = [
                e.payload.result
                for e in events
                if isinstance(e.payload, ToolExecutionCompletedPayload)
            ]
            assert len(results) == 2 and all(
                r.error_code is ToolErrorCode.APPROVAL_REJECTED for r in results
            )
            assert len(approval.requests) == 1
            assert not any(isinstance(e.payload, ToolExecutionStartedPayload) for e in events)
            assert not (case.workspace / "a.txt").exists()

    asyncio.run(scenario())


def test_directory_discovery_counts_skipped_batch_and_blocks_next_write_at_limit(
    case: RuntimeCase,
) -> None:
    nested = case.workspace / "nested"
    nested.mkdir(parents=True)
    (nested / "AGENTS.md").write_text("Use UTF-8 in this directory.", encoding="utf-8")
    count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal count
        count += 1
        if count == 1:
            return model_response(
                "",
                tuple(
                    ToolCall(name, "create_file", {"path": f"nested/{name}.txt", "content": name})
                    for name in ("a", "b")
                ),
            )
        assert "Use UTF-8" in request.content.decode()
        assert count == 2
        return model_response(
            "", (ToolCall("c", "create_file", {"path": "nested/c.txt", "content": "c"}),)
        )

    async def scenario() -> None:
        async with case.open(handler, options=RuntimeOptions(max_tool_calls=2)) as runtime:
            turn = await runtime.coordinator.send("Create files in nested.")
            assert turn.result.stop_reason is StopReason.LIMIT_REACHED
            assert (turn.result.model_calls, turn.result.tool_calls) == (2, 2)
            events = [e async for e in runtime.session.read(turn.session_id)]
            results = [
                e.payload.result
                for e in events
                if isinstance(e.payload, ToolExecutionCompletedPayload)
            ]
            assert [r.error_code for r in results] == [
                ToolErrorCode.INSTRUCTIONS_DISCOVERED,
                ToolErrorCode.NOT_EXECUTED,
                ToolErrorCode.NOT_EXECUTED,
            ]
            assert not any(isinstance(e.payload, ToolExecutionStartedPayload) for e in events)
            assert list(nested.glob("*.txt")) == []

    asyncio.run(scenario())


@pytest.mark.parametrize("when", ["before_request", "during_stream"])
def test_cancellation_closes_stream_records_one_terminal_and_releases_session(
    case: RuntimeCase, when: str
) -> None:
    async def scenario() -> None:
        entered, closed = asyncio.Event(), asyncio.Event()
        attempts = 0

        class PendingStream(httpx.AsyncByteStream):
            async def __aiter__(self) -> AsyncIterator[bytes]:
                entered.set()
                await asyncio.Event().wait()
                yield b""

            async def aclose(self) -> None:
                closed.set()

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal attempts
            del request
            attempts += 1
            return httpx.Response(200, stream=PendingStream())

        async with case.open(handler) as runtime:
            token = CancellationToken()
            if when == "before_request":
                token.cancel()
                turn = await runtime.coordinator.send(
                    "cancel", new_session_id="cancel", cancellation=token
                )
                assert turn.result.stop_reason is StopReason.CANCELLED
                assert attempts == 0
            else:
                task = asyncio.create_task(
                    runtime.coordinator.send("cancel", new_session_id="cancel")
                )
                try:
                    await asyncio.wait_for(entered.wait(), 3)
                    task.cancel()
                    with pytest.raises(asyncio.CancelledError):
                        await task
                    assert closed.is_set() and attempts == 1
                finally:
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
            events: list[RuntimeEvent] = [e async for e in runtime.session.read("cancel")]
            terminal = [
                e.payload
                for e in events
                if isinstance(e.payload, (TurnCompletedPayload, TurnFailedPayload))
            ]
            assert len(terminal) == 1 and terminal[0].stop_reason is StopReason.CANCELLED
            assert not any(isinstance(e.payload, ToolExecutionStartedPayload) for e in events)
        competitor = JsonlSession(case.paths.sessions_directory)
        await competitor.acquire("cancel")
        await competitor.release("cancel")

    asyncio.run(scenario())
