import asyncio
from collections.abc import AsyncIterator

from jdagent.domain.model import ModelCapabilities, ModelEvent, ModelRequest, TextDelta
from jdagent.maintenance import PrioritizedModel


def test_priority_slot_waits_for_underlying_stream_close_before_reuse() -> None:
    async def scenario() -> None:
        closing, release_close = asyncio.Event(), asyncio.Event()
        closed: list[str] = []

        class Model:
            capabilities = ModelCapabilities()

            async def stream(self, request: ModelRequest) -> AsyncIterator[ModelEvent]:
                try:
                    yield TextDelta(request.model)
                finally:
                    closing.set()
                    await release_close.wait()
                    closed.append(request.model)

        scheduled = PrioritizedModel(Model())
        first = scheduled.stream(ModelRequest("first", ()))
        assert await anext(first) == TextDelta("first")
        close = asyncio.create_task(first.aclose())
        try:
            await asyncio.wait_for(closing.wait(), timeout=2)
            assert not close.done(), "Scheduling slot released before the provider finished closing"
            release_close.set()
            await close
            assert closed == ["first"]
            second = scheduled.stream(ModelRequest("second", ()))
            assert await asyncio.wait_for(anext(second), timeout=2) == TextDelta("second")
            await second.aclose()
            assert closed == ["first", "second"]
        finally:
            release_close.set()
            await close

    asyncio.run(scenario())
