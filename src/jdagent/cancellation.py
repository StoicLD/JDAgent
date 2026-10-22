"""Delay cancellation until an already-started local transaction finishes."""

import asyncio
from collections.abc import Awaitable
from typing import TypeVar

T = TypeVar("T")


async def complete_before_cancel(operation: Awaitable[T]) -> T:
    """Keep ownership until local storage work has finished, including its threads.

    This is for finite local transactions, never provider requests. Cancelling an
    asyncio thread wait does not stop the underlying write or lock acquisition.
    """
    task = asyncio.ensure_future(operation)
    cancelled = False
    while True:
        try:
            result = await asyncio.shield(task)
        except asyncio.CancelledError:
            if task.cancelled():
                raise
            cancelled = True
            continue
        if cancelled:
            raise asyncio.CancelledError
        return result
