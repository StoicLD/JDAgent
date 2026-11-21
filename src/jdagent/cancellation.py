"""等待已经开始的本地事务完成，再将取消信号交还给调用方。"""

import asyncio
from collections.abc import Awaitable
from typing import TypeVar

T = TypeVar("T")


async def complete_before_cancel(operation: Awaitable[T]) -> T:
    """即使收到取消，也要等已经开始的本地写入或加锁完成，再把取消传给调用方。

    取消异步等待无法停止底层线程。这里只用于会结束的本地事务；网络请求由各自的超时机制管理。
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
