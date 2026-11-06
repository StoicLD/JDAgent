"""Real HTTP socket interoperation with a stdlib-only independent MCP peer."""

import asyncio
import gzip
import json
import threading
import time
from collections.abc import Generator
from contextlib import contextmanager, suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx2
import pytest
from mcp import Client, MCPError
from mcp.client.streamable_http import streamable_http_client
from mcp.types import CONNECTION_CLOSED

from jdagent.adapters.mcp_transport import BoundedHttpTransport, ResponseLimitExceeded


@dataclass
class TaskPeerState:
    """Independent remote state which survives individual client connections."""

    status: str = "working"
    created_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())
    ttl_ms: int | None = None
    honor_cancel: bool = False
    complete_on_cancel: bool = False
    missing: bool = False
    submissions: int = 0
    queries: int = 0
    cancellations: int = 0
    task_id: str = "private-http-task"
    poll_interval_ms: int = 100
    query_times: list[float] = field(default_factory=lambda: list[float]())

    def snapshot(self) -> dict[str, object]:
        return {
            "taskId": self.task_id,
            "status": self.status,
            "createdAt": self.created_at,
            "lastUpdatedAt": self.created_at,
            "ttlMs": self.ttl_ms,
            "pollIntervalMs": self.poll_interval_ms,
        }


@contextmanager
def wire_server(
    version: str,
    encoding: str,
    *,
    discover_gate: tuple[threading.Event, threading.Event] | None = None,
    expected_authorization: str | None = None,
    tool_error: str | None = None,
    requests: list[str] | None = None,
    task_state: TaskPeerState | None = None,
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
            if requests is not None:
                requests.append(method)
            result: dict[str, object]
            response: dict[str, object] = {"jsonrpc": "2.0", "id": message["id"]}
            if method == "server/discover":
                if discover_gate is not None:
                    discover_gate[0].set()
                    discover_gate[1].wait(10)
                result = {"supportedVersions": [version], "capabilities": {"tools": {}}}
                if task_state is not None:
                    result["capabilities"] = {
                        "tools": {},
                        "extensions": {"io.modelcontextprotocol/tasks": {}},
                    }
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
                if tool_error is not None or task_state is not None:
                    result["tools"] = [{"name": "read", "inputSchema": {"type": "object"}}]
                if message.get("params", {}).get("cursor") == "oversized":
                    length = 800_000 if encoding == "sse" else 2_000_000
                    result["tools"] = [
                        {
                            "name": "large",
                            "description": "x" * length,
                            "inputSchema": {"type": "object"},
                        }
                    ]
            elif method in {"tasks/get", "tasks/cancel"}:
                assert task_state is not None
                assert message["params"]["taskId"] == "private-http-task"
                assert self.headers["Mcp-Name"] == "private-http-task"
                assert self.headers["Mcp-Method"] == method
                if method == "tasks/cancel":
                    task_state.cancellations += 1
                    if task_state.complete_on_cancel:
                        task_state.status = "completed"
                    elif task_state.honor_cancel:
                        task_state.status = "cancelled"
                    result = {"resultType": "complete"}
                else:
                    task_state.queries += 1
                    task_state.query_times.append(time.monotonic())
                    result = {**task_state.snapshot(), "resultType": "complete"}
                    if task_state.status == "completed":
                        result["result"] = {
                            "resultType": "complete",
                            "content": [{"type": "text", "text": "remote task result"}],
                        }
                    elif task_state.status == "failed":
                        result["error"] = {"code": -32001, "message": "private-task-error"}
                    elif task_state.status == "input_required":
                        result["inputRequests"] = {"ask": {"method": "roots/list"}}
                if task_state.missing:
                    response["error"] = {"code": -32602, "message": "private-task-not-found"}
            else:
                assert method == "tools/call"
                length = 800_000 if encoding == "sse" else 2_000_000
                content = "ok" if message["params"]["name"] == "small" else "x" * length
                if tool_error is not None:
                    content = "timeout"
                result = {"resultType": "complete", "content": [{"type": "text", "text": content}]}
                if task_state is not None:
                    task_state.submissions += 1
                    result = {**task_state.snapshot(), "resultType": "task"}
                if tool_error == "server_timeout":
                    response["error"] = {"code": -32001, "message": "private-timeout-text"}
            if "error" not in response:
                response["result"] = result
            body = json.dumps(response).encode()
            content_type = "application/json"
            if method in {"tools/call", "tools/list"} and encoding == "sse":
                body = b":" + b" " * 400_000 + b"\n\nevent: message\ndata: " + body + b"\n\n"
                content_type = "text/event-stream"
            self.send_response(504 if method == "tools/call" and tool_error == "http504" else 200)
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
