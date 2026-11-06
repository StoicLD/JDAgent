import asyncio
import gzip
import json
import zlib
from collections.abc import AsyncIterator

import httpx2
import pytest
from mcp import Client, MCPError
from mcp.client.streamable_http import streamable_http_client

from jdagent.adapters.mcp_transport import (
    BoundedHttpTransport,
    ResponseEncodingError,
    ResponseLimitExceeded,
)


class ResponseChunks(httpx2.AsyncByteStream):
    def __init__(self, chunks: tuple[bytes, ...]) -> None:
        self.chunks = chunks
        self.closed = False
        self.chunks_received = 0

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for chunk in self.chunks:
            self.chunks_received += 1
            yield chunk

    async def aclose(self) -> None:
        self.closed = True


@pytest.mark.parametrize("limit", [16, 1 << 20])
@pytest.mark.parametrize("delta", [-1, 0, 1])
def test_http_rejects_oversized_response_during_consumption(limit: int, delta: int) -> None:
    length = limit + delta

    async def scenario() -> None:
        stream = ResponseChunks((b"x" * 8, b"x" * (length - 8)))

        def respond(request: httpx2.Request) -> httpx2.Response:
            return httpx2.Response(200, stream=stream)

        transport = BoundedHttpTransport(httpx2.MockTransport(respond), limit_bytes=limit)
        async with httpx2.AsyncClient(transport=transport) as client:
            async with client.stream("POST", "https://server.test/mcp", content=b"{}") as response:
                if length > limit:
                    with pytest.raises(ResponseLimitExceeded):
                        await response.aread()
                else:
                    assert await response.aread() == b"x" * length
        assert stream.closed

    asyncio.run(scenario())


@pytest.mark.parametrize("format_name", ["json", "gzip", "sse"])
def test_sdk_receives_bounded_http_results_and_rejects_rpc_overflow(format_name: str) -> None:
    async def scenario() -> None:
        tool_calls: list[str] = []
        streams: list[ResponseChunks] = []

        def respond(request: httpx2.Request) -> httpx2.Response:
            if request.method != "POST":
                return httpx2.Response(405)
            message = json.loads(request.content)
            if message["method"] == "server/discover":
                result: dict[str, object] = {
                    "supportedVersions": ["2026-07-28"],
                    "capabilities": {"tools": {}},
                }
                return httpx2.Response(
                    200, json={"jsonrpc": "2.0", "id": message["id"], "result": result}
                )
            if message["method"] == "tools/list":
                return httpx2.Response(
                    200,
                    json={
                        "jsonrpc": "2.0",
                        "id": message["id"],
                        "result": {
                            "tools": [],
                            "cacheScope": "private",
                            "resultType": "complete",
                            "ttlMs": 0,
                        },
                    },
                )
            assert message["method"] == "tools/call"
            name = message["params"]["name"]
            tool_calls.append(name)
            text = "ok" if name == "small" else "x" * (1000 if format_name == "sse" else 1800)
            body = json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": message["id"],
                    "result": {
                        "resultType": "complete",
                        "content": [{"type": "text", "text": text}],
                    },
                }
            ).encode()
            headers = {"content-type": "application/json"}
            if format_name == "gzip":
                body = gzip.compress(body)
                headers["content-encoding"] = "gzip"
            if format_name == "sse":
                # Each event is below the SDK's event cap; only the RPC sum exceeds it.
                body = b":" + b" " * 600 + b"\n\nevent: message\ndata: " + body + b"\n\n"
                headers["content-type"] = "text/event-stream"
            stream = ResponseChunks(tuple(body[i : i + 64] for i in range(0, len(body), 64)))
            streams.append(stream)
            return httpx2.Response(200, headers=headers, stream=stream)

        async with httpx2.AsyncClient(
            transport=BoundedHttpTransport(httpx2.MockTransport(respond), limit_bytes=1200)
        ) as http_client:
            rejected = False
            try:
                async with Client(
                    streamable_http_client("https://server.test/mcp", http_client=http_client),
                    cache=None,
                ) as client:
                    result = await client.call_tool("small", {})
                    assert result.content[0].type == "text"
                    assert result.content[0].text == "ok"
                    try:
                        await client.call_tool("large", {})
                    except MCPError:
                        rejected = True
            except* ResponseLimitExceeded:
                # JSON read errors leave the SDK group; SSE surfaces an RPC error.
                rejected = True
            assert rejected
        assert tool_calls == ["small", "large"]
        assert all(stream.closed for stream in streams)

    asyncio.run(scenario())


@pytest.mark.parametrize("limit", [64, 1 << 20])
@pytest.mark.parametrize("delta", [-1, 0, 1])
@pytest.mark.parametrize("encoding", ["gzip", "deflate"])
def test_http_limit_counts_decompressed_bytes(limit: int, delta: int, encoding: str) -> None:
    length = limit + delta

    async def scenario() -> None:
        compressed = (gzip.compress if encoding == "gzip" else zlib.compress)(b"x" * length)
        stream = ResponseChunks(tuple(compressed[i : i + 7] for i in range(0, len(compressed), 7)))

        def respond(request: httpx2.Request) -> httpx2.Response:
            return httpx2.Response(200, headers={"content-encoding": encoding}, stream=stream)

        async with httpx2.AsyncClient(
            transport=BoundedHttpTransport(httpx2.MockTransport(respond), limit_bytes=limit)
        ) as client:
            if length > limit:
                with pytest.raises(ResponseLimitExceeded):
                    await client.get("https://server.test/mcp")
            else:
                assert (await client.get("https://server.test/mcp")).content == b"x" * length
        assert stream.closed

    asyncio.run(scenario())


@pytest.mark.parametrize("body", [b"invalid gzip", gzip.compress(b"payload")[:-4]])
def test_http_rejects_corrupt_or_truncated_compression(body: bytes) -> None:
    async def scenario() -> None:
        stream = ResponseChunks((body,))

        def respond(request: httpx2.Request) -> httpx2.Response:
            return httpx2.Response(200, headers={"content-encoding": "gzip"}, stream=stream)

        async with httpx2.AsyncClient(
            transport=BoundedHttpTransport(httpx2.MockTransport(respond))
        ) as client:
            with pytest.raises(ResponseEncodingError):
                await client.get("https://server.test/mcp")
        assert stream.closed

    asyncio.run(scenario())


def test_http_overflow_stops_receiving_remaining_chunks() -> None:
    async def scenario() -> None:
        stream = ResponseChunks((b"x" * 65536,) * 128)

        def respond(request: httpx2.Request) -> httpx2.Response:
            return httpx2.Response(200, stream=stream)

        async with httpx2.AsyncClient(
            transport=BoundedHttpTransport(httpx2.MockTransport(respond))
        ) as client:
            with pytest.raises(ResponseLimitExceeded):
                await client.get("https://server.test/mcp")
        assert stream.chunks_received == 17
        assert stream.closed

    asyncio.run(scenario())
