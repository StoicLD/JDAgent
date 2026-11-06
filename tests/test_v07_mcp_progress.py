"""Elapsed waiting and optional progress are local observations, never deadlines."""

import asyncio
import json
import sys
import time
from pathlib import Path

import pytest

from jdagent.adapters.fake import FakeApproval, FakeModelPort
from jdagent.adapters.mcp_connection import SDKMCPConnection
from jdagent.composition import build_runtime
from jdagent.configuration import CliOverrides, resolve_configuration
from jdagent.data_paths import DataPaths
from jdagent.domain.events import MCPInvocationFinishedPayload
from jdagent.domain.mcp import MCPMethod
from jdagent.domain.model import ResponseCompleted, TextDelta, ToolCallCompleted
from jdagent.domain.tools import ApprovalDecision, ToolCall, ToolErrorCode
from jdagent.mcp_configuration import MCPServerConfiguration
from tests.test_v07_mcp_http import TaskPeerState, wire_server


def wait_for_signal(path: Path) -> bool:
    deadline = time.monotonic() + 5
    while not path.exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    return path.exists()


@pytest.mark.parametrize("engine", ["native", "langgraph"])
@pytest.mark.parametrize("progress", [False, True])
@pytest.mark.parametrize("finish", ["release", "deadline"])
def test_long_tool_exposes_elapsed_time_and_optional_progress_without_starting_another_turn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, engine: str, progress: bool, finish: str
) -> None:
    paths = DataPaths.for_workspace(
        tmp_path, config_root=tmp_path / "config", data_root=tmp_path / "data"
    )
    paths.user_config_file.parent.mkdir(parents=True)
    fixture = Path(__file__).parent / "fixtures" / "mcp_stdio_server.py"
    paths.user_config_file.write_text(
        "[memory]\nauto_consolidation = false\n"
        '[mcp.servers.docs]\nenabled = true\ntransport = "stdio"\n'
        f"command = {json.dumps(sys.executable)}\n"
        f"args = {json.dumps([str(fixture), '2026-07-28', 'catalog_once'])}\n"
        'tools = ["read"]\n',
        encoding="utf-8",
    )
    config = resolve_configuration(
        tmp_path,
        CliOverrides(provider="fake", runtime_engine=engine),
        data_paths=paths,
        environment={},
    )
    gate = tmp_path / "read"
    model = FakeModelPort(
        scripts=(
            (
                ToolCallCompleted(ToolCall("find", "search_tools", {"query": "read"})),
                ResponseCompleted("tool_calls"),
            ),
            (
                ToolCallCompleted(
                    ToolCall(
                        "read",
                        "mcp__docs__read",
                        {"fixture_gate": str(gate), "fixture_progress": progress},
                    )
                ),
                ResponseCompleted("tool_calls"),
            ),
            (TextDelta("Done."), ResponseCompleted("stop")),
        )
    )

    async def scenario() -> None:
        runtime = build_runtime(
            config, FakeApproval(ApprovalDecision.APPROVE), model_wrapper=lambda _: model
        )
        session_id = await runtime.coordinator.create_session()
        sending = asyncio.create_task(
            runtime.coordinator.send("Read the remote document", session_id=session_id)
        )
        try:
            assert await asyncio.to_thread(wait_for_signal, gate.with_suffix(".started"))
            original_clock = time.monotonic
            monkeypatch.setattr(time, "monotonic", lambda: original_clock() + 2)
            status = runtime.coordinator.mcp_status(session_id)[0]
            for _ in range(100):
                status = runtime.coordinator.mcp_status(session_id)[0]
                assert status.activity is not None
                if not progress or status.activity.progress is not None:
                    break
                await asyncio.sleep(0.01)
            activity = status.activity
            assert activity is not None
            assert activity.name == "read" and activity.operation == "tools/call"
            assert activity.elapsed_seconds >= 2
            assert activity.progress == (1 if progress else None)
            assert activity.total == (4 if progress else None)
            assert not sending.done() and len(model.requests) == 2
            if finish == "deadline":
                if progress:
                    monkeypatch.setattr(time, "monotonic", lambda: original_clock() + 500)
                    gate.with_suffix(".progress").touch()
                    update = runtime.coordinator.mcp_status(session_id)[0].activity
                    for _ in range(100):
                        update = runtime.coordinator.mcp_status(session_id)[0].activity
                        assert update is not None
                        if update.progress == 2:
                            break
                        await asyncio.sleep(0.01)
                    assert update is not None and update.progress == 2
                monkeypatch.setattr(time, "monotonic", lambda: original_clock() + 601)
            else:
                gate.with_suffix(".release").touch()
            result = await asyncio.wait_for(sending, 5)
            if finish == "release":
                assert result.result.stop_reason.value == "completed"
            else:
                events = [e async for e in runtime.session.read(session_id)]
                final = next(
                    e.payload
                    for e in events
                    if isinstance(e.payload, MCPInvocationFinishedPayload)
                    and e.payload.result.call_id == "read"
                )
                assert final.result.error_code is ToolErrorCode.TIMEOUT and final.result.stop_turn
                assert (
                    final.result.outcome is not None
                    and final.result.outcome.data["timeout_source"] == "client"
                )
                assert not final.result_confirmed and len(model.requests) == 2
            assert runtime.coordinator.mcp_status(session_id)[0].activity is None
            assert all(
                "private-progress-diagnostic" not in repr(request) for request in model.requests
            )
        finally:
            gate.with_suffix(".release").touch()
            if not sending.done():
                sending.cancel()
            await asyncio.gather(sending, return_exceptions=True)
            await runtime.aclose()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("version", "method"),
    [
        ("2026-07-28", "tools/call"),
        ("2025-11-25", "tools/call"),
        ("2026-07-28", "tasks/get"),
        ("2026-07-28", "tasks/cancel"),
    ],
)
def test_http_progress_uses_the_public_callback_for_business_and_task_requests(
    version: str, method: MCPMethod
) -> None:
    state = TaskPeerState(status="completed")
    with wire_server(
        version,
        "sse",
        task_state=state if method.startswith("tasks/") else None,
        progress_values=(2, 5),
    ) as url:
        configuration = MCPServerConfiguration(
            "docs", "http", True, ("read",), False, False, url=url
        )

        async def scenario() -> None:
            connection = SDKMCPConnection(configuration)
            observed: list[tuple[float, float | None]] = []

            async def progress(value: float, total: float | None) -> None:
                observed.append((value, total))

            try:
                result = await connection.request(
                    method,
                    {"name": "small", "arguments": {}}
                    if method == "tools/call"
                    else {"taskId": state.task_id},
                    on_progress=progress,
                )
                assert observed == [(2, 5)]
                assert "private-http-progress" not in repr(result)
            finally:
                assert await connection.aclose()

        asyncio.run(scenario())
