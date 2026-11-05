"""Await model iterator cleanup when a consumer ends before exhausting the stream."""

from collections.abc import AsyncGenerator, AsyncIterator
from contextlib import asynccontextmanager
from typing import Protocol, runtime_checkable

from jdagent.domain.model import ModelEvent


@runtime_checkable
class _ClosableStream(Protocol):
    async def aclose(self) -> None: ...


@asynccontextmanager
async def closing_model_stream(
    events: AsyncIterator[ModelEvent],
) -> AsyncGenerator[AsyncIterator[ModelEvent]]:
    """Close generators and closeable adapters; also accept ordinary async iterators."""
    try:
        yield events
    finally:
        if isinstance(events, _ClosableStream):
            await events.aclose()
