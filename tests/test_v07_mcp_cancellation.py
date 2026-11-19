"""MCP cancellation reaches the connection owner without an unbounded tool wait."""

import asyncio
import json
import os
import sys
import threading
import time
from pathlib import Path
from typing import Literal

import pytest

from jdagent.adapters.fake import FakeApproval, FakeModelPort
from jdagent.adapters.memory import InMemorySession
from jdagent.composition import build_runtime
from jdagent.configuration import CliOverrides, resolve_configuration
from jdagent.data_paths import DataPaths
from jdagent.domain.events import (
    MCPAttemptFinishedPayload,
    MCPInvocationFinishedPayload,
    MCPTaskObservedPayload,
    MCPTaskRegisteredPayload,
    RuntimeEvent,
    RuntimeEventType,
    SessionStartedPayload,
    ToolExecutionCompletedPayload,
    UserMessagePayload,
)
from jdagent.domain.json import JsonObject
from jdagent.domain.model import ResponseCompleted, TextDelta, ToolCallCompleted
from jdagent.domain.tools import (
    ApprovalDecision,
    RiskLevel,
    ToolCall,
    ToolDefinition,
    ToolErrorCode,
    ToolExecutionContext,
    ToolOrigin,
    ToolOutcome,
)
from jdagent.eventing import EventJournal
from jdagent.tools.permissions import DefaultPermissionPolicy
from jdagent.tools.runtime import ToolRegistry, ToolRuntime


@pytest.mark.parametrize("origin", ["mcp", "mcp_control", "builtin"])
def test_repeated_cancel_reaches_mcp_cleanup_but_keeps_local_transaction_ownership(
    tmp_path: Path, origin: Literal["mcp", "mcp_control", "builtin"]
) -> None:
    async def scenario() -> None:
        started = asyncio.Event()
        cleaning = asyncio.Event()
        release = asyncio.Event()

        async def operation(arguments: JsonObject, context: ToolExecutionContext) -> ToolOutcome:
            del arguments, context
            started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cleaning.set()
                await release.wait()
                return ToolOutcome(completed=False)
            raise AssertionError("An unreleased operation cannot finish")

        runtime = ToolRuntime(
            ToolRegistry(
                (
                    ToolDefinition(
                        "operation",
                        "Owned operation",
                        {"type": "object"},
                        RiskLevel.READ,
                        operation,
                        manages_timeout=True,
                        origin=ToolOrigin(origin),
                    ),
                )
            ),
            DefaultPermissionPolicy(),
            FakeApproval(ApprovalDecision.APPROVE),
        )
        running = asyncio.create_task(
            runtime.execute(
                ToolCall("call", "operation", {}), ToolExecutionContext("session", "turn", tmp_path)
            )
        )
        try:
            await asyncio.wait_for(started.wait(), 2)
            running.cancel()
            await asyncio.wait_for(cleaning.wait(), 2)
            running.cancel()
            done, _ = await asyncio.wait({running}, timeout=0.1)
            completed_before_release = bool(done)
        finally:
            release.set()
            result = await running
        assert completed_before_release is (origin != "builtin")
        assert result.error_code is ToolErrorCode.CANCELLED and result.stop_turn

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "boundary", ["registered", "result", "ordinary", "persist", "persist_task"]
)
def test_cancellation_after_receipt_keeps_the_received_handle_or_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, boundary: str
) -> None:
    publishing, release = threading.Event(), threading.Event()
    original_link = os.link

    def delayed_link(source: str | Path, destination: str | Path) -> None:
        content = Path(source).read_bytes()
        if b'"text":"ok"' in content or boundary == "persist_task" and b'"task":' in content:
            publishing.set()
            assert release.wait(5)
        original_link(source, destination)

    if boundary in {"persist", "persist_task"}:
        monkeypatch.setattr(os, "link", delayed_link)
    paths = DataPaths.for_workspace(
        tmp_path, config_root=tmp_path / "config", data_root=tmp_path / "data"
    )
    paths.user_config_file.parent.mkdir(parents=True)
    peer = Path(__file__).parent / "fixtures" / "mcp_stdio_server.py"
    paths.user_config_file.write_text(
        "[memory]\nauto_consolidation = false\n"
        '[mcp.servers.docs]\nenabled = true\ntransport = "stdio"\n'
        f"command = {json.dumps(sys.executable)}\n"
        f"args = {json.dumps([str(peer), '2026-07-28', 'catalog_once'])}\n"
        'tools = ["read"]\n',
        encoding="utf-8",
    )
    config = resolve_configuration(
        tmp_path,
        CliOverrides(provider="fake"),
        data_paths=paths,
        environment={},
    )
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
                        {} if boundary in {"ordinary", "persist"} else {"fixture_task": "normal"},
                    )
                ),
                ResponseCompleted("tool_calls"),
            ),
        )
    )

    async def scenario() -> None:
        class CancelAfterReceipt:
            async def observe(self, event: RuntimeEvent) -> None:
                payload = event.payload
                matches = (
                    isinstance(payload, MCPTaskRegisteredPayload)
                    if boundary == "registered"
                    else isinstance(payload, MCPAttemptFinishedPayload)
                    and payload.call_id == "read"
                    if boundary == "ordinary"
                    else isinstance(payload, MCPTaskObservedPayload)
                    and payload.status == "completed"
                    and boundary == "result"
                )
                if matches:
                    current = asyncio.current_task()
                    assert current is not None
                    current.cancel()
                    await asyncio.sleep(0)

        runtime = build_runtime(
            config,
            FakeApproval(ApprovalDecision.APPROVE),
            model_wrapper=lambda _: model,
            event_observers=(CancelAfterReceipt(),),
        )
        try:
            sending = asyncio.create_task(runtime.coordinator.send("Read the document"))
            if boundary in {"persist", "persist_task"}:
                assert await asyncio.to_thread(publishing.wait, 5)
                sending.cancel()
                await asyncio.sleep(0.02)
                sending.cancel()
                release.set()
            turn = await sending
            events = [event async for event in runtime.session.read(turn.session_id)]
            finished = next(
                event.payload
                for event in events
                if isinstance(event.payload, MCPInvocationFinishedPayload)
                and event.payload.result.call_id == "read"
            )
            assert finished.result_confirmed
            if boundary in {"registered", "persist_task"}:
                assert any(
                    isinstance(event.payload, MCPTaskObservedPayload)
                    and event.payload.status == "cancelled"
                    for event in events
                )
            else:
                assert finished.original_hash is not None
                assert (
                    "ok" if boundary in {"ordinary", "persist"} else "done; calls=1; polls=2"
                ) in json.dumps(finished.result.outcome.data if finished.result.outcome else {})
            result = next(
                event.payload.result
                for event in events
                if isinstance(event.payload, ToolExecutionCompletedPayload)
                and event.payload.result.call_id == "read"
            )
            assert result.error_code is ToolErrorCode.CANCELLED and result.stop_turn
            assert len(model.requests) == 2
        finally:
            release.set()
            await runtime.aclose()

    asyncio.run(scenario())


@pytest.mark.parametrize("timeout", ["default", "server", "tool"])
def test_ordinary_mcp_call_can_outlive_builtin_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, timeout: str
) -> None:
    paths = DataPaths.for_workspace(
        tmp_path, config_root=tmp_path / "config", data_root=tmp_path / "data"
    )
    paths.user_config_file.parent.mkdir(parents=True)
    peer = Path(__file__).parent / "fixtures" / "mcp_stdio_server.py"
    suffix = "" if timeout == "default" else "timeout_seconds = 30\n"
    if timeout == "tool":
        suffix = (
            "timeout_seconds = 1\n[mcp.servers.docs.tool_overrides.read]\ntimeout_seconds = 30\n"
        )
    paths.user_config_file.write_text(
        "[memory]\nauto_consolidation = false\n"
        '[mcp.servers.docs]\nenabled = true\ntransport = "stdio"\n'
        f"command = {json.dumps(sys.executable)}\n"
        f"args = {json.dumps([str(peer), '2026-07-28', 'catalog_once'])}\n"
        'tools = ["read"]\n' + suffix,
        encoding="utf-8",
    )
    config = resolve_configuration(
        tmp_path,
        CliOverrides(provider="fake"),
        data_paths=paths,
        environment={},
    )
    gate = str(tmp_path / "gate")

    def wait_for_request() -> None:
        deadline = time.monotonic() + 5
        while not Path(gate + ".started").exists():
            if time.monotonic() >= deadline:
                raise TimeoutError("The independent peer did not receive the request")
            time.sleep(0.01)

    model = FakeModelPort(
        scripts=(
            (
                ToolCallCompleted(ToolCall("find", "search_tools", {"query": "read"})),
                ResponseCompleted("tool_calls"),
            ),
            (
                ToolCallCompleted(ToolCall("read", "mcp__docs__read", {"fixture_gate": gate})),
                ResponseCompleted("tool_calls"),
            ),
            (TextDelta("Read completed."), ResponseCompleted("stop")),
        )
    )

    async def scenario() -> None:
        loop = asyncio.get_running_loop()
        real_time = loop.time
        elapsed = 0.0
        monkeypatch.setattr(loop, "time", lambda: real_time() + elapsed)
        runtime = build_runtime(
            config, FakeApproval(ApprovalDecision.APPROVE), model_wrapper=lambda _: model
        )
        sending = asyncio.create_task(runtime.coordinator.send("Read the document"))
        try:
            await asyncio.to_thread(wait_for_request)
            # The request is already on the wire. Advance the application's
            # clock beyond the builtin limit without waiting eleven real seconds.
            elapsed = 11
            await asyncio.sleep(0.05)
            still_waiting = not sending.done()
            await asyncio.to_thread(Path(gate + ".release").touch)
            turn = await asyncio.wait_for(sending, 5)
            events = [event async for event in runtime.session.read(turn.session_id)]
            result = next(
                event.payload.result
                for event in events
                if isinstance(event.payload, ToolExecutionCompletedPayload)
                and event.payload.result.call_id == "read"
            )
            assert still_waiting
            assert result.error_code is None
            assert len(model.requests) == 3
        finally:
            await asyncio.to_thread(Path(gate + ".release").touch)
            if not sending.done():
                sending.cancel()
                await sending
            await runtime.aclose()
            monkeypatch.setattr(loop, "time", real_time)

    asyncio.run(scenario())


def test_cancel_during_event_append_finishes_the_local_transaction_before_returning() -> None:
    async def scenario() -> None:
        entered = asyncio.Event()
        release = asyncio.Event()

        class DelayedStorage(InMemorySession):
            async def append(self, event: RuntimeEvent) -> None:
                if event.sequence == 2:
                    entered.set()
                    await release.wait()
                await super().append(event)

        session = DelayedStorage()
        journal = await EventJournal.open(session, "session")
        await journal.record(None, RuntimeEventType.SESSION_STARTED, SessionStartedPayload())
        pending = asyncio.create_task(
            journal.record("turn", RuntimeEventType.USER_MESSAGE, UserMessagePayload("original"))
        )
        await entered.wait()
        pending.cancel()
        pending.cancel()
        done, _ = await asyncio.wait({pending}, timeout=0.02)
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await pending
        assert not done
        assert len(journal.events) == 2
        await journal.record("next", RuntimeEventType.USER_MESSAGE, UserMessagePayload("next"))
        persisted = [event async for event in session.read("session")]
        assert tuple(persisted) == journal.events
        assert [event.sequence for event in persisted] == [1, 2, 3]

    asyncio.run(scenario())
