"""消费者提前结束时，等待模型响应流释放资源。"""

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
    """消费者提前退出或被取消时，等待可关闭的模型流释放资源；普通异步迭代器也可使用。"""
    try:
        yield events
    finally:
        if isinstance(events, _ClosableStream):
            await events.aclose()
