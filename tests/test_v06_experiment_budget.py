import asyncio
import json
import sqlite3
from collections.abc import AsyncIterator
from dataclasses import replace
from pathlib import Path

import httpx
import pytest

from jdagent.adapters.deepseek import DeepSeekModelPort
from jdagent.adapters.fake import FakeModelPort
from jdagent.adapters.langchain_model import LangChainModelPort
from jdagent.context_budget import InputEstimator
from jdagent.domain.json import JsonObject
from jdagent.domain.model import (
    MessageRole,
    ModelEvent,
    ModelFailed,
    ModelMessage,
    ModelRequest,
    ModelSettings,
    ResponseCompleted,
    TextDelta,
    Usage,
    UsageReported,
)
from tests.experiment_budget import (
    BudgetedModel,
    ExperimentBudgetError,
    ExperimentLedger,
    ExperimentLimits,
)

AdapterType = type[DeepSeekModelPort] | type[LangChainModelPort]


def request(identity: str, kind: str = "task") -> ModelRequest:
    return ModelRequest(
        "deepseek-test",
        (ModelMessage(MessageRole.USER, "hello"),),
        settings=ModelSettings(max_output_tokens=100),
        metadata={"request_id": identity, "call_kind": kind},
    )


def response(*, usage: bool = True, duplicate: bool = False) -> bytes:
    chunks: list[JsonObject] = [
        {"choices": [{"index": 0, "delta": {"content": "done"}, "finish_reason": "stop"}]},
    ]
    if usage:
        chunks.append(
            {
                "choices": [],
                "usage": {"prompt_tokens": 12, "completion_tokens": 4, "total_tokens": 16},
            }
        )
        if duplicate:
            chunks.append(chunks[-1])
    return (
        "".join(f"data: {json.dumps(chunk)}\n\n" for chunk in chunks) + "data: [DONE]\n\n"
    ).encode()


async def collect(events: AsyncIterator[ModelEvent]) -> list[ModelEvent]:
    return [event async for event in events]


@pytest.mark.parametrize("adapter_type", [DeepSeekModelPort, LangChainModelPort])
@pytest.mark.parametrize("kind", ["task", "compaction", "citation_repair", "memory"])
def test_every_call_kind_is_reserved_before_http_and_blocked_when_shared_calls_are_exhausted(
    tmp_path: Path, adapter_type: AdapterType, kind: str
) -> None:
    ledger = ExperimentLedger(tmp_path / "budget.sqlite", "fixed", ExperimentLimits(1, 10000, 1000))
    attempts = 0

    def handler(http_request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        del http_request
        attempts += 1
        (entry,) = ledger.entries()
        assert entry.status == "reserved" and entry.call_kind == kind
        return httpx.Response(200, content=response(duplicate=True))

    async def scenario() -> None:
        async with httpx.AsyncClient(
            base_url="https://test", transport=httpx.MockTransport(handler)
        ) as client:
            adapter = adapter_type("dummy-key", base_url="https://test", client=client)
            try:
                model = BudgetedModel(adapter, ledger, "native/native", 1)
                events = await collect(model.stream(request("allowed", kind)))
                assert not any(isinstance(event, ModelFailed) for event in events)
                reopened = ExperimentLedger(ledger.path, "fixed", ledger.limits)
                other_attempt = BudgetedModel(adapter, reopened, "langgraph/langchain", 2)
                blocked = await collect(other_attempt.stream(request("blocked", kind)))
                assert len(blocked) == 1 and isinstance(blocked[0], ModelFailed)
                assert blocked[0].provider_code == "experiment_budget"
                assert attempts == 1
                (entry,) = reopened.entries()
                assert (entry.input_tokens, entry.output_tokens, entry.status) == (12, 4, "settled")
            finally:
                await adapter.aclose()

    asyncio.run(scenario())


@pytest.mark.parametrize("adapter_type", [DeepSeekModelPort, LangChainModelPort])
@pytest.mark.parametrize("limit", ["input", "output"])
def test_token_limit_blocks_transport_before_first_request(
    tmp_path: Path, adapter_type: AdapterType, limit: str
) -> None:
    limits = ExperimentLimits(
        10, 1 if limit == "input" else 10000, 99 if limit == "output" else 1000
    )
    ledger = ExperimentLedger(tmp_path / "budget.sqlite", "fixed", limits)
    attempts = 0

    def handler(http_request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        del http_request
        attempts += 1
        return httpx.Response(200, content=response())

    async def scenario() -> None:
        async with httpx.AsyncClient(
            base_url="https://test", transport=httpx.MockTransport(handler)
        ) as client:
            adapter = adapter_type("dummy-key", base_url="https://test", client=client)
            try:
                events = await collect(
                    BudgetedModel(adapter, ledger, "native/native", 1).stream(request("blocked"))
                )
                assert len(events) == 1 and isinstance(events[0], ModelFailed)
                assert attempts == 0 and not ledger.entries()
            finally:
                await adapter.aclose()

    asyncio.run(scenario())


@pytest.mark.parametrize("adapter_type", [DeepSeekModelPort, LangChainModelPort])
@pytest.mark.parametrize("failure", ["missing_usage", "http_503", "broken_stream"])
def test_unknown_usage_keeps_reservation_across_reopen_without_hidden_retry(
    tmp_path: Path, adapter_type: AdapterType, failure: str
) -> None:
    ledger = ExperimentLedger(tmp_path / "budget.sqlite", "fixed", ExperimentLimits(3, 10000, 100))
    attempts = 0

    class BrokenResponse(httpx.AsyncByteStream):
        async def __aiter__(self) -> AsyncIterator[bytes]:
            yield b'data: {"choices":[{"index":0,"delta":{"content":"partial"}}]}\n\n'
            raise httpx.ReadError("offline stream failure")

    def handler(http_request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        del http_request
        attempts += 1
        if failure == "http_503":
            return httpx.Response(503, json={"error": {"message": "offline failure"}})
        if failure == "broken_stream":
            return httpx.Response(200, stream=BrokenResponse())
        return httpx.Response(200, content=response(usage=False))

    async def scenario() -> None:
        async with httpx.AsyncClient(
            base_url="https://test", transport=httpx.MockTransport(handler)
        ) as client:
            adapter = adapter_type("dummy-key", base_url="https://test", client=client)
            try:
                model = BudgetedModel(adapter, ledger, "native/native", 1)
                # A transport exception may be returned as ModelFailed or raised by an SDK.
                try:
                    await collect(model.stream(request("unknown")))
                except httpx.ReadError:
                    assert failure == "broken_stream"
                reopened = ExperimentLedger(ledger.path, "fixed", ledger.limits)
                (entry,) = reopened.entries()
                assert entry.status == "unknown"
                assert (
                    entry.input_tokens == InputEstimator().measure(request("unknown")).input_tokens
                )
                assert entry.output_tokens == 100
                blocked = await collect(
                    BudgetedModel(adapter, reopened, "langgraph/langchain", 2).stream(
                        request("later")
                    )
                )
                assert isinstance(blocked[0], ModelFailed)
                assert attempts == 1
            finally:
                await adapter.aclose()

    asyncio.run(scenario())


def test_crash_reservation_duplicate_id_and_settlement_are_not_refunded_or_double_counted(
    tmp_path: Path,
) -> None:
    path = tmp_path / "budget.sqlite"
    limits = ExperimentLimits(3, 10000, 1000)
    ledger = ExperimentLedger(path, "fixed", limits)
    ledger.reserve(request("crashed"), "native/native", 1)
    reopened = ExperimentLedger(path, "fixed", limits)
    assert reopened.entries()[0].status == "reserved"
    with pytest.raises(ExperimentBudgetError, match="already admitted"):
        reopened.reserve(request("crashed"), "langgraph/langchain", 2)
    reopened.reserve(request("done"), "langgraph/langchain", 2)
    assert not reopened.finish("done", Usage(10, 5), drained=True)
    assert not reopened.finish("done", Usage(10, 5), drained=True)
    assert [entry.output_tokens for entry in reopened.entries()] == [100, 5]
    with pytest.raises(ExperimentBudgetError, match="already finalized"):
        reopened.finish("done", Usage(1, 1), drained=True)
    with pytest.raises(ExperimentBudgetError, match="identity or limits"):
        ExperimentLedger(path, "fixed", ExperimentLimits(4, 10000, 1000))
    with pytest.raises(ExperimentBudgetError, match="identity or limits"):
        ExperimentLedger(path, "different", limits)


def test_concurrent_instances_cannot_reserve_the_same_last_slot(tmp_path: Path) -> None:
    limits = ExperimentLimits(1, 10000, 1000)
    first = ExperimentLedger(tmp_path / "budget.sqlite", "fixed", limits)
    second = ExperimentLedger(first.path, "fixed", limits)

    async def scenario() -> None:
        results = await asyncio.gather(
            asyncio.to_thread(first.reserve, request("one"), "native/native", 1),
            asyncio.to_thread(second.reserve, request("two"), "langgraph/langchain", 2),
            return_exceptions=True,
        )
        assert sum(isinstance(value, str) for value in results) == 1
        assert sum(isinstance(value, ExperimentBudgetError) for value in results) == 1
        assert len(first.entries()) == 1

    asyncio.run(scenario())


@pytest.mark.parametrize("content", [b"", b"corrupted"])
def test_existing_incomplete_or_corrupt_ledger_is_not_reset(tmp_path: Path, content: bytes) -> None:
    path = tmp_path / "budget.sqlite"
    path.write_bytes(content)
    with pytest.raises((ExperimentBudgetError, sqlite3.DatabaseError)):
        ExperimentLedger(path, "fixed", ExperimentLimits(1, 100, 100))


def test_partial_usage_does_not_release_reservation_and_overage_stops_later_calls(
    tmp_path: Path,
) -> None:
    ledger = ExperimentLedger(tmp_path / "budget.sqlite", "fixed", ExperimentLimits(3, 200, 100))
    ledger.reserve(request("partial"), "native/native", 1)
    assert not ledger.finish("partial", Usage(10, 1), drained=False)
    assert ledger.entries()[0].status == "unknown"
    assert ledger.entries()[0].output_tokens == 100
    with pytest.raises(ExperimentBudgetError, match="exhausted"):
        ledger.reserve(request("next"), "native/native", 2)


def test_provider_usage_over_limit_is_retained_and_marks_response_as_failed(tmp_path: Path) -> None:
    ledger = ExperimentLedger(tmp_path / "budget.sqlite", "fixed", ExperimentLimits(3, 100, 1000))
    provider = FakeModelPort(
        scripts=((TextDelta("done"), ResponseCompleted("stop"), UsageReported(Usage(101, 2))),)
    )
    model = BudgetedModel(provider, ledger, "native/native", 1)

    async def scenario() -> None:
        events = await collect(model.stream(request("overage")))
        assert isinstance(events[-1], ModelFailed)
        assert ledger.entries()[0].input_tokens == 101
        later = await collect(model.stream(request("later")))
        assert isinstance(later[0], ModelFailed)
        assert len(provider.requests) == 1

    asyncio.run(scenario())


@pytest.mark.parametrize("invalid", ["missing_id", "unbounded_output", "unknown_kind"])
def test_invalid_request_cannot_bypass_experiment_admission(tmp_path: Path, invalid: str) -> None:
    ledger = ExperimentLedger(tmp_path / "budget.sqlite", "fixed", ExperimentLimits(1, 10000, 1000))
    original = request("one")
    bad = {
        "missing_id": replace(original, metadata={}),
        "unbounded_output": replace(original, settings=ModelSettings()),
        "unknown_kind": request("one", "hidden_retry"),
    }[invalid]
    model = FakeModelPort(scripts=((ResponseCompleted("stop"),),))

    async def scenario() -> None:
        events = await collect(BudgetedModel(model, ledger, "native/native", 1).stream(bad))
        assert isinstance(events[0], ModelFailed)
        assert not model.requests and not ledger.entries()

    asyncio.run(scenario())


@pytest.mark.parametrize("adapter_type", [DeepSeekModelPort, LangChainModelPort])
def test_cancellation_closes_http_and_preserves_unknown_reservation(
    tmp_path: Path, adapter_type: AdapterType
) -> None:
    ledger = ExperimentLedger(tmp_path / "budget.sqlite", "fixed", ExperimentLimits(3, 10000, 100))

    async def scenario() -> None:
        entered, closed = asyncio.Event(), asyncio.Event()
        attempts = 0

        class PendingResponse(httpx.AsyncByteStream):
            async def __aiter__(self) -> AsyncIterator[bytes]:
                entered.set()
                await asyncio.Event().wait()
                yield b""

            async def aclose(self) -> None:
                closed.set()

        def handler(http_request: httpx.Request) -> httpx.Response:
            nonlocal attempts
            del http_request
            attempts += 1
            return httpx.Response(200, stream=PendingResponse())

        async with httpx.AsyncClient(
            base_url="https://test", transport=httpx.MockTransport(handler)
        ) as client:
            adapter = adapter_type("dummy-key", base_url="https://test", client=client)
            model = BudgetedModel(adapter, ledger, "native/native", 1)
            task = asyncio.create_task(collect(model.stream(request("cancelled"))))
            try:
                await asyncio.wait_for(entered.wait(), timeout=2)
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
                assert closed.is_set()
                (entry,) = ledger.entries()
                assert entry.status == "unknown" and entry.output_tokens == 100
                blocked = await collect(model.stream(request("next")))
                assert isinstance(blocked[0], ModelFailed)
                assert attempts == 1
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                await adapter.aclose()

    asyncio.run(scenario())
