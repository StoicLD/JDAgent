"""通过标准输入输出传送有大小上限的消息，由 MCP SDK 解析协议。"""

import os
import signal
import subprocess
from collections.abc import AsyncGenerator, Mapping
from contextlib import asynccontextmanager, suppress
from functools import partial
from pathlib import Path

import anyio
from anyio.abc import ByteReceiveStream, ByteSendStream
from anyio.streams.file import FileReadStream, FileWriteStream
from anyio.streams.memory import MemoryObjectReceiveStream, MemoryObjectSendStream
from anyio.to_thread import run_sync
from mcp import types
from mcp.client.stdio import get_default_environment
from mcp.shared.message import SessionMessage

from jdagent.adapters.mcp_transport import ResponseEncodingError, ResponseLimitExceeded
from jdagent.adapters.windows_process import WindowsCommand

StdioStreams = tuple[
    MemoryObjectReceiveStream[SessionMessage | Exception], MemoryObjectSendStream[SessionMessage]
]


@asynccontextmanager
async def _owned_pipes(
    command: str, args: tuple[str, ...], cwd: Path, environment: dict[str, str]
) -> AsyncGenerator[tuple[ByteSendStream, ByteReceiveStream, ByteReceiveStream]]:
    """启动并管理标准输入输出连接使用的子进程，调用方退出时回收进程和管道。"""
    if os.name == "nt":
        # 即使创建子进程时收到取消，也要先取得进程的控制权，才能可靠地回收它。
        job: WindowsCommand | None = None
        with anyio.CancelScope(shield=True):
            job = await run_sync(
                partial(WindowsCommand, [command, *args], cwd, environment, keep_stdin=True)
            )
        assert job is not None
        try:
            yield FileWriteStream(job.stdin), FileReadStream(job.stdout), FileReadStream(job.stderr)
        finally:
            # 先关闭 Job 终止所有后代进程，阻塞在管道上的读取才能结束。
            with anyio.CancelScope(shield=True):
                await run_sync(job.close)
    else:
        with anyio.CancelScope(shield=True):
            process = await anyio.open_process(
                [command, *args],
                cwd=cwd,
                env=environment,
                stderr=subprocess.PIPE,
                start_new_session=True,
            )
        assert process.stdin and process.stdout and process.stderr
        try:
            yield process.stdin, process.stdout, process.stderr
        finally:
            with anyio.CancelScope(shield=True):
                with suppress(ProcessLookupError):
                    os.killpg(process.pid, signal.SIGKILL)
                with anyio.fail_after(5):
                    await process.aclose()


@asynccontextmanager
async def bounded_stdio_client(
    command: str,
    args: tuple[str, ...] = (),
    *,
    cwd: Path,
    environment: Mapping[str, str] | None = None,
    limit_bytes: int = 1 << 20,
) -> AsyncGenerator[StdioStreams]:
    """启动并管理通过标准输入输出通信的 MCP 服务进程，在 SDK 解析前检查消息边界和大小。

    子进程只继承 SDK 要求的最少环境和显式配置。宿主读走标准错误以防阻塞，不展示远端日志。
    """
    if limit_bytes <= 0:
        raise ValueError("Receive limit must be positive")
    incoming, read = anyio.create_memory_object_stream[SessionMessage | Exception](0)
    write, outgoing = anyio.create_memory_object_stream[SessionMessage](0)

    async def receive(stdout: ByteReceiveStream) -> None:
        buffer = bytearray()
        async with incoming:
            try:
                while True:
                    try:
                        chunk = await stdout.receive(min(65536, limit_bytes + 1 - len(buffer)))
                    except anyio.EndOfStream:
                        if buffer:
                            raise ResponseEncodingError("Incomplete MCP stdio message") from None
                        return
                    buffer.extend(chunk)
                    while (end := buffer.find(b"\n")) >= 0:
                        line = bytes(buffer[:end])
                        del buffer[: end + 1]
                        try:
                            message = types.jsonrpc_message_adapter.validate_json(
                                line, by_name=False
                            )
                        except ValueError:
                            raise ResponseEncodingError("Invalid MCP stdio message") from None
                        await incoming.send(SessionMessage(message))
                    if len(buffer) > limit_bytes:
                        raise ResponseLimitExceeded("MCP stdio message exceeds the receive limit")
            except (ResponseLimitExceeded, ResponseEncodingError, OSError) as error:
                await incoming.send(error)
            except (anyio.BrokenResourceError, anyio.ClosedResourceError):
                return

    async def send(stdin: ByteSendStream) -> None:
        try:
            async with outgoing:
                async for message in outgoing:
                    encoded = message.message.model_dump_json(by_alias=True, exclude_unset=True)
                    await stdin.send((encoded + "\n").encode("utf-8"))
        except (anyio.BrokenResourceError, anyio.ClosedResourceError, OSError):
            await incoming.aclose()

    async def discard(stderr: ByteReceiveStream) -> None:
        with suppress(anyio.EndOfStream, anyio.BrokenResourceError, anyio.ClosedResourceError):
            while True:
                await stderr.receive(65536)

    async with anyio.create_task_group() as tasks:
        async with _owned_pipes(
            command, args, cwd, get_default_environment() | dict(environment or {})
        ) as pipes:
            tasks.start_soon(receive, pipes[1])
            tasks.start_soon(send, pipes[0])
            tasks.start_soon(discard, pipes[2])
            try:
                yield read, write
            finally:
                tasks.cancel_scope.cancel()
                read.close()
                write.close()
