"""Request snapshots remain independent during overlap and foreground scheduling."""

import asyncio
import json
from dataclasses import replace

import httpx
import pytest

from jdagent.adapters.deepseek import DeepSeekModelPort
from jdagent.adapters.langchain_model import LangChainModelPort
from jdagent.domain.model import (
    MessageRole,
    ModelEvent,
    ModelFailed,
    ModelMessage,
    ModelRequest,
    ModelSettings,
    ResponseCompleted,
)
from jdagent.maintenance import PrioritizedModel
from jdagent.ports import ModelPort
from tests.runtime_matrix import model_response


@pytest.mark.parametrize("adapter_type", [DeepSeekModelPort, LangChainModelPort])
@pytest.mark.parametrize("scheduled", [False, True], ids=["overlap", "foreground_priority"])
def test_model_and_maintenance_snapshots_survive_overlapping_new_settings(
    adapter_type: type[DeepSeekModelPort] | type[LangChainModelPort], scheduled: bool
) -> None:
    async def scenario() -> None:
        entered, release = asyncio.Event(), asyncio.Event()
        background_ready, foreground_ready = asyncio.Event(), asyncio.Event()
        captured: list[httpx.Request] = []
        active = maximum_active = 0

        async def handler(request: httpx.Request) -> httpx.Response:
            nonlocal active, maximum_active
            captured.append(request)
            active += 1
            maximum_active = max(maximum_active, active)
            try:
                if json.loads(request.content)["model"] == "blocking":
                    entered.set()
                    await release.wait()
                return model_response('{"ok":true}')
            finally:
                active -= 1

        async with httpx.AsyncClient(
            base_url="https://test", transport=httpx.MockTransport(handler)
        ) as client:
            adapter = adapter_type("dummy-key", base_url="https://test", client=client)
            model: ModelPort = PrioritizedModel(adapter) if scheduled else adapter
            base = ModelRequest(
                "blocking",
                (ModelMessage(MessageRole.USER, "Return a JSON object."),),
                settings=ModelSettings(max_output_tokens=100, timeout_seconds=3, temperature=0.1),
                metadata={"call_kind": "task"},
            )
            pending = replace(
                base,
                model="old-maintenance",
                settings=ModelSettings(max_output_tokens=200, timeout_seconds=7, temperature=0.2),
                metadata={"call_kind": "memory"},
                provider_options={"deepseek": {"response_format": {"type": "json_object"}}},
            )
            updated = replace(
                base,
                model="new-foreground",
                settings=ModelSettings(max_output_tokens=300, timeout_seconds=11, temperature=0.3),
            )

            async def collect(request: ModelRequest, ready: asyncio.Event) -> list[ModelEvent]:
                ready.set()
                return [event async for event in model.stream(request)]

            tasks = [asyncio.create_task(collect(base, asyncio.Event()))]
            try:
                await asyncio.wait_for(entered.wait(), 3)
                tasks.append(asyncio.create_task(collect(pending, background_ready)))
                await asyncio.wait_for(background_ready.wait(), 3)
                tasks.append(asyncio.create_task(collect(updated, foreground_ready)))
                await asyncio.wait_for(foreground_ready.wait(), 3)
                if scheduled:
                    assert len(captured) == 1
                release.set()
                outputs = await asyncio.wait_for(asyncio.gather(*tasks), 5)
                for events in outputs:
                    assert not any(isinstance(e, ModelFailed) for e in events)
                    assert sum(isinstance(e, ResponseCompleted) for e in events) == 1
            finally:
                release.set()
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
                await adapter.aclose()
            assert not client.is_closed
        bodies = [json.loads(r.content) for r in captured]
        if scheduled:
            assert [b["model"] for b in bodies] == ["blocking", "new-foreground", "old-maintenance"]
            assert maximum_active == 1
        else:
            assert maximum_active >= 2
        assert len(bodies) == 3
        expected = {
            "blocking": (100, 3, 0.1),
            "old-maintenance": (200, 7, 0.2),
            "new-foreground": (300, 11, 0.3),
        }
        for request, body in zip(captured, bodies, strict=True):
            maximum, timeout, temperature = expected[body["model"]]
            assert body["max_tokens"] == maximum and body["temperature"] == temperature
            assert request.extensions["timeout"]["read"] == timeout
            assert "tools" not in body
            if body["model"] == "old-maintenance":
                assert body["response_format"] == {"type": "json_object"}

    asyncio.run(scenario())
