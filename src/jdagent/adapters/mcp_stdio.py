"""Bounded stdio framing; the official SDK owns JSON-RPC and MCP semantics."""

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
    if os.name == "nt":
        # Ownership must be established even if cancellation races with spawning.
        job: WindowsCommand | None = None
        with anyio.CancelScope(shield=True):
            job = await run_sync(
                partial(WindowsCommand, [command, *args], cwd, environment, keep_stdin=True)
            )
        assert job is not None
        try:
            yield FileWriteStream(job.stdin), FileReadStream(job.stdout), FileReadStream(job.stderr)
        finally:
            # Closing the Job kills descendants before waiting for blocked pipe readers.
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
    """Own one server tree and reject oversize/unfinished messages before SDK parsing.

    Only the SDK's minimal inherited environment and explicit overrides are passed.
    Stderr is drained without exposing remote log content. Enter/exit in the same task.
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
