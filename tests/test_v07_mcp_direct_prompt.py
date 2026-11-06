"""Explicit Prompt use shares material retrieval without inventing model tool calls."""

import asyncio
import json
import sys
from dataclasses import replace
from pathlib import Path

import pytest

from jdagent.adapters.fake import FakeApproval, FakeModelPort
from jdagent.adapters.scripted_terminal import CapturePresenter, ScriptedTerminal
from jdagent.application.headless import ExitStatus, run_headless
from jdagent.application.interactive import (
    InteractiveApplication,
    InteractiveContext,
    parse_user_action,
)
from jdagent.application.session_catalog import SessionCatalog
from jdagent.composition import build_runtime
from jdagent.configuration import CliOverrides, ResolvedConfiguration, resolve_configuration
from jdagent.data_paths import DataPaths, workspace_identity
from jdagent.domain.events import MCPInvocationFinishedPayload, RuntimeEventType, UserMessagePayload
from jdagent.domain.model import ResponseCompleted, TextDelta, ToolCallCompleted
from jdagent.domain.tools import ApprovalDecision, ToolCall


@pytest.mark.parametrize("engine", ["native", "langgraph"])
def test_direct_prompt_obeys_reloaded_user_result_quota(tmp_path: Path, engine: str) -> None:
    config = prompt_configuration(tmp_path, engine)
    path = config.data_paths.user_config_file
    original_config = path.read_text(encoding="utf-8")
    model = FakeModelPort(scripts=((TextDelta("Explained."), ResponseCompleted("stop")),))

    async def scenario() -> None:
        runtime = build_runtime(
            config, FakeApproval(ApprovalDecision.APPROVE), model_wrapper=lambda _: model
        )
        try:
            path.write_text("[mcp]\nresult_quota_mib = 1\n" + original_config, encoding="utf-8")
            rejected = await run_headless(
                runtime.coordinator,
                "/mcp prompt docs explain topic=quota",
                provider="fake",
                model="fake",
            )
            assert rejected.exit_status is ExitStatus.RUNTIME_ERROR
            assert not model.requests
            events = [event async for event in runtime.session.read(rejected.session_id)]
            assert not any(
                event.event_type is RuntimeEventType.MCP_INVOCATION_STARTED for event in events
            )
            path.write_text("[mcp]\nresult_quota_mib = 8\n" + original_config, encoding="utf-8")
            accepted = await run_headless(
                runtime.coordinator,
                "/mcp prompt docs explain topic=quota",
                session_id=rejected.session_id,
                provider="fake",
                model="fake",
            )
            assert accepted.exit_status is ExitStatus.SUCCESS and len(model.requests) == 1
        finally:
            await runtime.aclose()

    asyncio.run(scenario())


def prompt_configuration(tmp_path: Path, engine: str) -> ResolvedConfiguration:
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
        "prompts = true\n",
        encoding="utf-8",
    )
    return resolve_configuration(
        tmp_path,
        CliOverrides(provider="fake", runtime_engine=engine),
        data_paths=paths,
        environment={},
    )


@pytest.mark.parametrize("engine", ["native", "langgraph"])
def test_headless_direct_prompt_keeps_template_separate_from_user_facts_after_reopen(
    tmp_path: Path, engine: str
) -> None:
    config = prompt_configuration(tmp_path, engine)
    command = '/mcp prompt docs explain topic="MCP connections"'
    model = FakeModelPort(
        scripts=((TextDelta("Explained."), ResponseCompleted("stop")),), repeat_last=True
    )

    async def scenario() -> None:
        runtime = build_runtime(
            config, FakeApproval(ApprovalDecision.APPROVE), model_wrapper=lambda _: model
        )
        try:
            result = await run_headless(runtime.coordinator, command, provider="fake", model="fake")
            assert result.exit_status is ExitStatus.SUCCESS
            material = [m for m in model.requests[0].messages if m.name == "mcp_material"]
            assert (
                len(material) == 1
                and "External template for: MCP connections" in material[0].content
            )
            assert not any(m.tool_calls or m.tool_call_id for m in model.requests[0].messages)
            events = [e async for e in runtime.session.read(result.session_id)]
            assert [
                e.payload.content for e in events if isinstance(e.payload, UserMessagePayload)
            ] == [command]
            assert sum(e.event_type.value == "mcp_prompt_prepared" for e in events) == 1
            assert not any(e.event_type is RuntimeEventType.TOOL_CALL_REQUESTED for e in events)
        finally:
            await runtime.aclose()
        reopened = build_runtime(
            config, FakeApproval(ApprovalDecision.APPROVE), model_wrapper=lambda _: model
        )
        try:
            await reopened.coordinator.send(
                "Continue the explanation", session_id=result.session_id
            )
            material = [m for m in model.requests[-1].messages if m.name == "mcp_material"]
            assert (
                len(material) == 1
                and "External template for: MCP connections" in material[0].content
            )
            assert "Protected original user statements" not in material[0].content
        finally:
            await reopened.aclose()

    asyncio.run(scenario())


@pytest.mark.parametrize("engine", ["native", "langgraph"])
def test_direct_prompt_wait_can_be_cancelled_before_starting_a_model(
    tmp_path: Path, engine: str
) -> None:
    config = prompt_configuration(tmp_path, engine)
    gate = tmp_path / "prompt-call"
    command = f'/mcp prompt docs explain topic="fixture_gate:{gate.as_posix()}"'
    model = FakeModelPort(scripts=((TextDelta("Must not run"), ResponseCompleted("stop")),))

    async def scenario() -> None:
        runtime = build_runtime(
            config, FakeApproval(ApprovalDecision.APPROVE), model_wrapper=lambda _: model
        )
        running = asyncio.create_task(
            run_headless(runtime.coordinator, command, provider="fake", model="fake")
        )
        try:
            async with asyncio.timeout(5):
                while not gate.with_suffix(".started").exists():  # noqa: ASYNC110 - child process signal
                    await asyncio.sleep(0.01)
            running.cancel()
            result = await asyncio.wait_for(running, 7)
            assert result.exit_status is ExitStatus.CANCELLED and not model.requests
            events = [e async for e in runtime.session.read(result.session_id)]
            finished = next(
                e.payload for e in events if isinstance(e.payload, MCPInvocationFinishedPayload)
            )
            assert not finished.result_confirmed and finished.result.stop_turn
            assert runtime.coordinator.mcp_status(result.session_id)[0].connection != "ready"
            assert not any(e.event_type is RuntimeEventType.TOOL_CALL_REQUESTED for e in events)
        finally:
            gate.with_suffix(".release").touch()
            if not running.done():
                running.cancel()
            await asyncio.gather(running, return_exceptions=True)
            await runtime.aclose()

    asyncio.run(scenario())


@pytest.mark.parametrize("engine", ["native", "langgraph"])
def test_direct_prompt_preview_fits_remaining_window_and_original_remains_readable(
    tmp_path: Path, engine: str
) -> None:
    config = prompt_configuration(tmp_path, engine)
    config = resolve_configuration(
        tmp_path,
        replace(config.cli_overrides, max_context_tokens=12000, max_output_tokens=512),
        data_paths=config.data_paths,
        environment={},
    )
    model = FakeModelPort(scripts=((TextDelta("Explained."), ResponseCompleted("stop")),))

    async def scenario() -> None:
        runtime = build_runtime(
            config, FakeApproval(ApprovalDecision.APPROVE), model_wrapper=lambda _: model
        )
        try:
            result = await run_headless(
                runtime.coordinator,
                "/mcp prompt docs explain topic=fixture_large",
                provider="fake",
                model="fake",
            )
            assert result.exit_status is ExitStatus.SUCCESS
            assert len(model.requests) == 1
            material = next(m for m in model.requests[0].messages if m.name == "mcp_material")
            projected = json.loads(material.content)["data"]
            assert not projected["preview_complete"] and len(material.content) < 6000
            original = projected["original"]
            assert original["bytes"] > 20000
        finally:
            await runtime.aclose()
        reader = FakeModelPort(
            scripts=(
                (
                    ToolCallCompleted(
                        ToolCall(
                            "page",
                            "read_mcp_result",
                            {"sha256": original["sha256"], "offset": 0, "limit": 2048},
                        )
                    ),
                    ResponseCompleted("tool_calls"),
                ),
                (TextDelta("Read original page."), ResponseCompleted("stop")),
            )
        )
        read_config = resolve_configuration(
            tmp_path,
            replace(config.cli_overrides, max_context_tokens=64000),
            data_paths=config.data_paths,
            environment={},
        )
        reopened = build_runtime(
            read_config, FakeApproval(ApprovalDecision.APPROVE), model_wrapper=lambda _: reader
        )
        try:
            await reopened.coordinator.send(
                "Read the original template", session_id=result.session_id
            )
            page = next(m for m in reader.requests[-1].messages if m.tool_call_id == "page")
            data = json.loads(page.content)["data"]
            assert data["original"] == original and "remote-only" in data["text"]
            assert data["source"]["capability_kind"] == "prompt"
        finally:
            await reopened.aclose()

    asyncio.run(scenario())


@pytest.mark.parametrize("engine", ["native", "langgraph"])
@pytest.mark.parametrize("missing", [False, True])
def test_interactive_prompt_command_fetches_material_or_asks_for_missing_arguments(
    tmp_path: Path, engine: str, missing: bool
) -> None:
    config = prompt_configuration(tmp_path, engine)
    command = "/mcp prompt docs explain" + ("" if missing else ' topic="interactive topic"')
    model = FakeModelPort(
        scripts=(
            (TextDelta("What topic?" if missing else "Explained."), ResponseCompleted("stop")),
        )
    )

    async def scenario() -> None:
        runtime = build_runtime(
            config, FakeApproval(ApprovalDecision.APPROVE), model_wrapper=lambda _: model
        )
        presenter = CapturePresenter()
        catalog = SessionCatalog(
            session=runtime.session,
            discovery=runtime.session,
            workspace_identity=workspace_identity(tmp_path),
        )
        app = InteractiveApplication(
            terminal=ScriptedTerminal((parse_user_action(command), parse_user_action("/exit"))),
            presenter=presenter,
            coordinator=runtime.coordinator,
            catalog=catalog,
            session=runtime.session,
            context=InteractiveContext("fake", "fake", tmp_path, "ask"),
        )
        try:
            assert await app.run() == 0
            assert len(model.requests) == 1
            material = next(m for m in model.requests[0].messages if m.name == "mcp_material")
            assert (
                '"missing_arguments": ["topic"]'
                if missing
                else "External template for: interactive topic"
            ) in material.content
            assert ("What topic?" if missing else "Explained.") in "\n".join(
                e.message for e in presenter.events
            )
            sessions = await catalog.list_sessions()
            events = [e async for e in runtime.session.read(sessions[0].session_id)]
            assert [
                e.payload.content for e in events if isinstance(e.payload, UserMessagePayload)
            ] == [command]
            assert not any(e.event_type is RuntimeEventType.TOOL_CALL_REQUESTED for e in events)
            assert any(e.event_type is RuntimeEventType.MCP_INVOCATION_STARTED for e in events) is (
                not missing
            )
        finally:
            await runtime.aclose()

    asyncio.run(scenario())


@pytest.mark.parametrize("engine", ["native", "langgraph"])
@pytest.mark.parametrize(
    ("command", "category"),
    [
        ("/mcp prompt docs explain", "mcp_prompt_invalid_arguments"),
        ("/mcp prompt docs absent", "mcp_prompt_tool_unavailable"),
    ],
)
def test_headless_prompt_errors_are_explicit_without_waiting_for_input_or_model(
    tmp_path: Path, engine: str, command: str, category: str
) -> None:
    config = prompt_configuration(tmp_path, engine)
    model = FakeModelPort(scripts=((TextDelta("Must not run"), ResponseCompleted("stop")),))

    async def scenario() -> None:
        runtime = build_runtime(
            config, FakeApproval(ApprovalDecision.APPROVE), model_wrapper=lambda _: model
        )
        try:
            result = await run_headless(runtime.coordinator, command, provider="fake", model="fake")
            assert result.exit_status is ExitStatus.RUNTIME_ERROR
            assert result.error_category == category and not model.requests
            assert result.json_data()["status"] == "error"
            if category == "mcp_prompt_invalid_arguments":
                assert "topic" in result.answer
        finally:
            await runtime.aclose()

    asyncio.run(scenario())
