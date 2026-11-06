"""Real HTTP socket interoperation with a stdlib-only independent MCP peer."""

import asyncio
import gzip
import json
import threading
from collections.abc import Generator
from contextlib import contextmanager, suppress
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx2
import pytest
from mcp import Client, MCPError
from mcp.client.streamable_http import streamable_http_client
from mcp.types import CONNECTION_CLOSED

from jdagent.adapters.mcp_transport import BoundedHttpTransport, ResponseLimitExceeded


@contextmanager
def wire_server(
    version: str,
    encoding: str,
    *,
    discover_gate: tuple[threading.Event, threading.Event] | None = None,
    expected_authorization: str | None = None,
) -> Generator[str]:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: object) -> None:
            pass

        def do_GET(self) -> None:
            self.send_error(405)

        def do_DELETE(self) -> None:
            self.send_response(200)
            self.end_headers()

        def do_POST(self) -> None:
            message = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            if (
                expected_authorization is not None
                and self.headers.get("Authorization") != expected_authorization
            ):
                self.send_error(401)
                return
            if "id" not in message:
                self.send_response(202)
                self.end_headers()
                return
            method = message["method"]
            result: dict[str, object]
            response: dict[str, object] = {"jsonrpc": "2.0", "id": message["id"]}
            if method == "server/discover":
                if discover_gate is not None:
                    discover_gate[0].set()
                    discover_gate[1].wait(10)
                result = {"supportedVersions": [version], "capabilities": {"tools": {}}}
                if version == "2025-11-25":
                    response["error"] = {"code": -32601, "message": "Method not found"}
            elif method == "initialize":
                result = {
                    "protocolVersion": version,
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "wire-peer", "version": "1"},
                }
            elif method == "tools/list":
                result = {
                    "resultType": "complete",
                    "cacheScope": "private",
                    "ttlMs": 0,
                    "tools": [],
                }
                if message.get("params", {}).get("cursor") == "oversized":
                    length = 800_000 if encoding == "sse" else 2_000_000
                    result["tools"] = [
                        {
                            "name": "large",
                            "description": "x" * length,
                            "inputSchema": {"type": "object"},
                        }
                    ]
            else:
                assert method == "tools/call"
                length = 800_000 if encoding == "sse" else 2_000_000
                content = "ok" if message["params"]["name"] == "small" else "x" * length
                result = {"resultType": "complete", "content": [{"type": "text", "text": content}]}
            if "error" not in response:
                response["result"] = result
            body = json.dumps(response).encode()
            content_type = "application/json"
            if method in {"tools/call", "tools/list"} and encoding == "sse":
                body = b":" + b" " * 400_000 + b"\n\nevent: message\ndata: " + body + b"\n\n"
                content_type = "text/event-stream"
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            if encoding == "gzip":
                body = gzip.compress(body)
                self.send_header("Content-Encoding", "gzip")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            with suppress(BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                for offset in range(0, len(body), 65536):
                    self.wfile.write(body[offset : offset + 65536])
                    self.wfile.flush()

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    worker = threading.Thread(
        target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True
    )
    worker.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/mcp"
    finally:
        if discover_gate is not None:
            discover_gate[1].set()
        server.shutdown()
        server.server_close()
        worker.join(2)
        assert not worker.is_alive()


@pytest.mark.parametrize("version", ["2026-07-28", "2025-11-25"])
@pytest.mark.parametrize("encoding", ["json", "gzip", "sse"])
def test_http_wire_peer_negotiation_and_receive_limit(version: str, encoding: str) -> None:
    async def scenario(url: str) -> None:
        rejected = False
        async with httpx2.AsyncClient(
            transport=BoundedHttpTransport(httpx2.AsyncHTTPTransport(retries=0)),
            timeout=httpx2.Timeout(3),
            trust_env=False,
        ) as http:
            try:
                async with Client(
                    streamable_http_client(url, http_client=http),
                    cache=None,
                    read_timeout_seconds=3,
                ) as client:
                    assert client.protocol_version == version
                    result = await client.call_tool("small", {})
                    assert result.content[0].type == "text" and result.content[0].text == "ok"
                    try:
                        await client.call_tool("large", {})
                    except MCPError as error:
                        assert error.code == CONNECTION_CLOSED
                        rejected = True
            except* ResponseLimitExceeded:
                rejected = True
        assert rejected

    with wire_server(version, encoding) as url:
        asyncio.run(scenario(url))
