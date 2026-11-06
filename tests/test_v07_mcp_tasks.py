import asyncio
import json
import sys
from pathlib import Path

import httpx2
import pytest
from mcp import Client
from mcp.client.streamable_http import streamable_http_client

from jdagent.adapters.mcp_stdio import bounded_stdio_client
from jdagent.adapters.mcp_tasks import TaskHandle, TasksExtension, cancel_task, get_task
from jdagent.adapters.mcp_transport import BoundedHttpTransport


def test_task_handle_is_returned_before_polling_and_cancel_is_only_acknowledgement() -> None:
    async def scenario() -> None:
        verbs: list[str] = []
        cancelled = False

        def respond(request: httpx2.Request) -> httpx2.Response:
            nonlocal cancelled
            if request.method != "POST":
                return httpx2.Response(405)
            message = json.loads(request.content)
            method = message["method"]
            verbs.append(method)
            result: dict[str, object]
            task: dict[str, object] = {
                "taskId": "remote-1",
                "status": "working",
                "createdAt": "2026-11-06T00:00:00Z",
                "lastUpdatedAt": "2026-11-06T00:00:01Z",
                "ttlMs": None,
                "pollIntervalMs": 100,
            }
            if method == "server/discover":
                result = {
                    "supportedVersions": ["2026-07-28"],
                    "capabilities": {
                        "tools": {},
                        "extensions": {"io.modelcontextprotocol/tasks": {}},
                    },
                }
            else:
                capabilities = message["params"]["_meta"][
                    "io.modelcontextprotocol/clientCapabilities"
                ]
                assert "io.modelcontextprotocol/tasks" in capabilities["extensions"]
                if method == "tools/call":
                    result = {**task, "resultType": "task"}
                else:
                    assert message["params"]["taskId"] == "remote-1"
                    assert request.headers["Mcp-Name"] == "remote-1"
                    assert request.headers["Mcp-Method"] == method
                    if method == "tasks/cancel":
                        cancelled = True
                        result = {"resultType": "complete"}
                    else:
                        assert method == "tasks/get"
                        result = {**task, "resultType": "complete"}
                        if not cancelled:
                            result.update(
                                {
                                    "status": "completed",
                                    "result": {
                                        "resultType": "complete",
                                        "content": [],
                                        "isError": True,
                                    },
                                }
                            )
            return httpx2.Response(
                200, json={"jsonrpc": "2.0", "id": message["id"], "result": result}
            )

        async with httpx2.AsyncClient(
            transport=BoundedHttpTransport(httpx2.MockTransport(respond))
        ) as http:
            async with Client(
                streamable_http_client("https://server.test/mcp", http_client=http),
                extensions=[TasksExtension()],
                cache=None,
            ) as client:
                handle = await client.session.call_tool("long", {}, allow_claimed=True)
                assert isinstance(handle, TaskHandle)
                assert handle.task_id == "remote-1"
                assert verbs == ["server/discover", "tools/call"]
                state = await get_task(client.session, handle.task_id)
                assert state.status == "completed"
                assert state.result is not None and state.result.is_error
                await cancel_task(client.session, handle.task_id)
                still_working = await get_task(client.session, handle.task_id)
                assert still_working.status == "working"
                assert verbs.count("tools/call") == 1

    asyncio.run(scenario())


@pytest.mark.parametrize("version", ["2026-07-28", "2025-11-25"])
def test_stdio_tasks_are_modern_only(version: str) -> None:
    async def scenario() -> None:
        fixture = Path(__file__).parent / "fixtures" / "mcp_stdio_server.py"
        async with Client(
            bounded_stdio_client(sys.executable, (str(fixture), version), cwd=fixture.parent),
            extensions=[TasksExtension()],
            cache=None,
            read_timeout_seconds=3,
        ) as client:
            if version == "2025-11-25":
                with pytest.raises(ValueError, match="does not support modern Tasks"):
                    await get_task(client.session, "stdio-task")
                result = await client.call_tool("small", {})
                assert result.content[0].type == "text" and result.content[0].text == "ok"
            else:
                handle = await client.session.call_tool("task", {}, allow_claimed=True)
                assert isinstance(handle, TaskHandle)
                result = await get_task(client.session, handle.task_id)
                assert result.status == "completed" and result.result is not None
                assert result.result.content[0].type == "text"
                assert result.result.content[0].text == "done"

    asyncio.run(scenario())
