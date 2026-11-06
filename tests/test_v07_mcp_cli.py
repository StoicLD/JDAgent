"""Foreground controls stay reachable while the single input reader awaits approval."""

import asyncio
import json
import sys
import time
from pathlib import Path

import pytest
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput

from jdagent.adapters.fake import FakeModelPort
from jdagent.adapters.terminal import PromptToolkitTerminal
from jdagent.application.approval import ScopedApproval
from jdagent.application.interactive import (
    InteractionRuntimeObserver,
    InteractionState,
    InteractiveApplication,
    InteractiveContext,
    UiEvent,
)
from jdagent.application.session_catalog import SessionCatalog
from jdagent.composition import build_runtime
from jdagent.configuration import CliOverrides, resolve_configuration
from jdagent.data_paths import DataPaths, workspace_identity
from jdagent.domain.events import (
    MCPInvocationFinishedPayload,
    MCPInvocationStartedPayload,
    RuntimeEventType,
)
from jdagent.domain.model import ResponseCompleted, TextDelta, ToolCallCompleted
from jdagent.domain.tools import ToolCall


@pytest.mark.parametrize("engine", ["native", "langgraph"])
@pytest.mark.parametrize("finish", ["approve", "cancel", "exit"])
def test_status_compaction_cancel_and_exit_do_not_consume_the_approval_answer(
    tmp_path: Path, engine: str, finish: str
) -> None:
    paths = DataPaths.for_workspace(
        tmp_path, config_root=tmp_path / "config", data_root=tmp_path / "data"
    )
    paths.user_config_file.parent.mkdir(parents=True)
    paths.user_config_file.write_text("[memory]\nauto_consolidation = false\n", encoding="utf-8")
    config = resolve_configuration(
        tmp_path,
        CliOverrides(provider="fake", runtime_engine=engine),
        data_paths=paths,
        environment={},
    )
    model = FakeModelPort(
        scripts=(
            (
                ToolCallCompleted(
                    ToolCall("write", "create_file", {"path": "note.txt", "content": "approved"})
                ),
                ResponseCompleted("tool_calls"),
            ),
            (TextDelta("Created the file."), ResponseCompleted("stop")),
        )
    )

    async def scenario() -> None:
        updates: asyncio.Queue[UiEvent] = asyncio.Queue()
        seen: list[UiEvent] = []

        class Presenter:
            async def publish(self, event: UiEvent) -> None:
                seen.append(event)
                updates.put_nowait(event)

        async def until(text: str) -> UiEvent:
            async with asyncio.timeout(5):
                while True:
                    event = await updates.get()
                    if text in event.message:
                        return event

        with create_pipe_input() as pipe:
            terminal = PromptToolkitTerminal(
                history_file=tmp_path / "history", input=pipe, output=DummyOutput()
            )
            observer = InteractionRuntimeObserver()
            runtime = build_runtime(
                config,
                ScopedApproval(terminal),
                model_wrapper=lambda _: model,
                event_observers=(observer,),
            )
            application = InteractiveApplication(
                terminal=terminal,
                presenter=Presenter(),
                coordinator=runtime.coordinator,
                catalog=SessionCatalog(
                    session=runtime.session,
                    discovery=runtime.session,
                    workspace_identity=workspace_identity(tmp_path),
                ),
                session=runtime.session,
                context=InteractiveContext("fake", "fake", tmp_path, "ask"),
            )
            observer.bind(application)
            running = asyncio.create_task(application.run())
            try:
                await until(InteractionState.IDLE.value)
                pipe.send_text("Create note.txt\n")
                await until(InteractionState.WAITING_APPROVAL.value)
                for command, response in (
                    ("/status", "provider=fake"),
                    ("/context-window", "input_budget="),
                    ("/mcp status", "No MCP Servers configured"),
                    ("/compact", "/compact requires an idle session"),
                ):
                    pipe.send_text(command + "\n")
                    await until(response)
                assert not await asyncio.to_thread((tmp_path / "note.txt").exists)
                assert len(model.requests) == 1
                pipe.send_text(
                    "o\n"
                    if finish == "approve"
                    else "/cancel\n"
                    if finish == "cancel"
                    else "/exit\n"
                )
                if finish != "exit":
                    await until(InteractionState.IDLE.value)
                    pipe.send_text("/exit\n")
                assert await asyncio.wait_for(running, 5) == 0
                assert await asyncio.to_thread((tmp_path / "note.txt").exists) is (
                    finish == "approve"
                )
                summaries = await SessionCatalog(
                    session=runtime.session,
                    discovery=runtime.session,
                    workspace_identity=workspace_identity(tmp_path),
                ).list_sessions()
                events = [e async for e in runtime.session.read(summaries[0].session_id)]
                assert not any(
                    e.event_type is RuntimeEventType.COMPACTION_COMMITTED for e in events
                )
                assert not any("queued" in e.message for e in seen)
                assert len(model.requests) == (2 if finish == "approve" else 1)
            finally:
                application.cancel_current_turn()
                if not running.done():
                    pipe.send_bytes(b"\x04")
                await asyncio.gather(running, return_exceptions=True)
                await runtime.aclose()

    asyncio.run(scenario())


@pytest.mark.parametrize("engine", ["native", "langgraph"])
@pytest.mark.parametrize("finish", ["cancel", "exit"])
def test_long_mcp_call_accepts_local_controls_and_closes_before_cli_exit(
    tmp_path: Path, engine: str, finish: str
) -> None:
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
        CliOverrides(provider="fake", runtime_engine=engine),
        data_paths=paths,
        environment={},
    )
    gate = str(tmp_path / "gate")
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
        )
    )

    def wait_for_request() -> None:
        deadline = time.monotonic() + 5
        while not Path(gate + ".started").exists():
            if time.monotonic() >= deadline:
                raise TimeoutError("The independent peer did not receive the request")
            time.sleep(0.01)

    async def scenario() -> None:
        updates: asyncio.Queue[UiEvent] = asyncio.Queue()

        class Presenter:
            async def publish(self, event: UiEvent) -> None:
                updates.put_nowait(event)

        async def until(text: str) -> None:
            async with asyncio.timeout(5):
                while text not in (await updates.get()).message:
                    pass

        with create_pipe_input() as pipe:
            terminal = PromptToolkitTerminal(
                history_file=tmp_path / "history", input=pipe, output=DummyOutput()
            )
            observer = InteractionRuntimeObserver()
            runtime = build_runtime(
                config,
                ScopedApproval(terminal),
                model_wrapper=lambda _: model,
                event_observers=(observer,),
            )
            catalog = SessionCatalog(
                session=runtime.session,
                discovery=runtime.session,
                workspace_identity=workspace_identity(tmp_path),
            )
            application = InteractiveApplication(
                terminal=terminal,
                presenter=Presenter(),
                coordinator=runtime.coordinator,
                catalog=catalog,
                session=runtime.session,
                context=InteractiveContext("fake", "fake", tmp_path, "ask"),
            )
            observer.bind(application)
            running = asyncio.create_task(application.run())
            try:
                await until(InteractionState.IDLE.value)
                pipe.send_text("Read the document\n")
                await asyncio.to_thread(wait_for_request)
                for command, response in (
                    ("/status", "provider=fake"),
                    ("/mcp status", "server=docs connection=ready"),
                    ("/context-window", "input_budget="),
                    ("/compact", "/compact requires an idle session"),
                    ("/mcp refresh docs", "Session is busy"),
                    ("/exit extra", "Usage: /exit"),
                    ("/cancel extra", "Usage: /cancel"),
                ):
                    pipe.send_text(command + "\n")
                    await until(response)
                pipe.send_text("/" + finish + "\n")
                if finish == "cancel":
                    await until(InteractionState.IDLE.value)
                    pipe.send_text("/exit\n")
                assert await asyncio.wait_for(running, 6) == 0
                sessions = await catalog.list_sessions()
                events = [e async for e in runtime.session.read(sessions[0].session_id)]
                attempts = [
                    e
                    for e in events
                    if isinstance(e.payload, MCPInvocationStartedPayload)
                    and e.payload.call_id == "read"
                ]
                ended = next(
                    e.payload
                    for e in events
                    if isinstance(e.payload, MCPInvocationFinishedPayload)
                    and e.payload.result.call_id == "read"
                )
                assert len(attempts) == 1 and not ended.result_confirmed
                assert ended.result.outcome is not None and ended.result.outcome.cancelled
                assert not any(
                    e.event_type is RuntimeEventType.COMPACTION_COMMITTED for e in events
                )
                assert len(model.requests) == 2
                assert (
                    runtime.coordinator.mcp_status(sessions[0].session_id)[0].connection == "closed"
                )
            finally:
                await asyncio.to_thread(Path(gate + ".release").touch)
                application.cancel_current_turn()
                if not running.done():
                    pipe.send_bytes(b"\x04")
                await asyncio.gather(running, return_exceptions=True)
                await runtime.aclose()

    asyncio.run(scenario())
