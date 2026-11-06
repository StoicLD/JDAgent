import asyncio
import ctypes
import os
import sys
from ctypes import wintypes
from pathlib import Path

import pytest
from mcp import Client, MCPError
from mcp.types import CONNECTION_CLOSED

from jdagent.adapters.mcp_stdio import bounded_stdio_client


@pytest.mark.parametrize("version", ["2026-07-28", "2025-11-25"])
def test_stdio_negotiates_and_rejects_unterminated_oversize(version: str) -> None:
    async def scenario() -> None:
        fixture = Path(__file__).parent / "fixtures" / "mcp_stdio_server.py"
        async with Client(
            bounded_stdio_client(
                sys.executable, (str(fixture), version), cwd=fixture.parent, limit_bytes=1200
            ),
            cache=None,
            read_timeout_seconds=3,
        ) as client:
            assert client.protocol_version == version
            result = await client.call_tool("small", {})
            assert result.content[0].type == "text"
            assert result.content[0].text == "ok"
            with pytest.raises(MCPError) as error:
                await client.call_tool("unterminated", {})
            assert error.value.code == CONNECTION_CLOSED

    asyncio.run(scenario())


@pytest.mark.parametrize("size", [(1 << 20) - 1, 1 << 20, (1 << 20) + 1])
def test_stdio_one_mib_boundary(size: int) -> None:
    async def scenario() -> None:
        fixture = Path(__file__).parent / "fixtures" / "mcp_stdio_server.py"
        async with Client(
            bounded_stdio_client(sys.executable, (str(fixture), "2026-07-28"), cwd=fixture.parent),
            cache=None,
            read_timeout_seconds=3,
        ) as client:
            if size > 1 << 20:
                with pytest.raises(MCPError) as error:
                    await client.call_tool("sized", {"bytes": size})
                assert error.value.code == CONNECTION_CLOSED
            else:
                result = await client.call_tool("sized", {"bytes": size})
                assert result.content[0].type == "text"
                assert result.content[0].text == "ok"

    asyncio.run(scenario())


def test_stdio_overflow_does_not_disable_another_server() -> None:
    async def scenario() -> None:
        fixture = Path(__file__).parent / "fixtures" / "mcp_stdio_server.py"

        def connection() -> Client:
            return Client(
                bounded_stdio_client(
                    sys.executable,
                    (str(fixture), "2026-07-28"),
                    cwd=fixture.parent,
                    limit_bytes=1200,
                ),
                cache=None,
                read_timeout_seconds=3,
            )

        async with connection() as healthy:
            async with connection() as faulty:
                with pytest.raises(MCPError) as error:
                    await faulty.call_tool("unterminated", {})
                assert error.value.code == CONNECTION_CLOSED
            result = await healthy.call_tool("small", {})
            assert result.content[0].type == "text" and result.content[0].text == "ok"

    asyncio.run(scenario())


@pytest.mark.skipif(os.name != "nt", reason="Windows Job ownership evidence")
@pytest.mark.parametrize("cancel", [False, True])
def test_stdio_exit_and_cancellation_terminate_server_tree(cancel: bool) -> None:
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel.WaitForSingleObject.restype = wintypes.DWORD
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    handles: list[int] = []

    async def scenario() -> None:
        ready = asyncio.Event()
        fixture = Path(__file__).parent / "fixtures" / "mcp_stdio_server.py"

        async def run() -> None:
            async with Client(
                bounded_stdio_client(
                    sys.executable, (str(fixture), "2026-07-28"), cwd=fixture.parent
                ),
                cache=None,
            ) as client:
                result = await client.call_tool("spawn_child", {})
                assert result.content[0].type == "text"
                for pid in result.content[0].text.split(","):
                    handle = kernel.OpenProcess(0x100000, False, int(pid))
                    assert handle and kernel.WaitForSingleObject(handle, 0) == 258
                    handles.append(handle)
                ready.set()
                if cancel:
                    await client.call_tool("hang", {})

        task = asyncio.create_task(run())
        await asyncio.wait_for(ready.wait(), 5)
        if cancel:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            await task

    try:
        asyncio.run(scenario())
        assert len(handles) == 2
        assert all(kernel.WaitForSingleObject(handle, 5000) == 0 for handle in handles)
    finally:
        for handle in handles:
            kernel.CloseHandle(handle)
