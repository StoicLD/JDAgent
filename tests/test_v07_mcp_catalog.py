import asyncio
import json
import sys
from pathlib import Path

import pytest

from jdagent.adapters.fake import FakeApproval
from jdagent.composition import RuntimeOptions, build_runtime
from jdagent.configuration import CliOverrides, resolve_configuration
from jdagent.data_paths import DataPaths
from jdagent.domain.mcp import MCPDirectoryLimits
from jdagent.domain.tools import ApprovalDecision


@pytest.mark.parametrize("engine", ["native", "langgraph"])
def test_session_discovers_only_enabled_capabilities_and_reconnects_after_release(
    tmp_path: Path, engine: str
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
        'tools = ["search", "read"]\nresources = true\nprompts = true\n',
        encoding="utf-8",
    )
    config = resolve_configuration(
        tmp_path,
        CliOverrides(provider="fake", runtime_engine=engine),
        data_paths=paths,
        environment={},
    )

    async def scenario() -> None:
        runtime = build_runtime(config, FakeApproval(ApprovalDecision.APPROVE))
        try:
            turn = await runtime.coordinator.send("hello")
            before = runtime.coordinator.mcp_status(turn.session_id)
            assert before[0].connection == "not_connected"
            with pytest.raises(ValueError, match="query"):
                await runtime.coordinator.mcp_search(turn.session_id, "")
            assert runtime.coordinator.mcp_status(turn.session_id)[0].connection == "not_connected"
            directory = await runtime.coordinator.mcp_list(turn.session_id)
            assert {(entry.kind, entry.name) for entry in directory.entries} == {
                ("tool", "search"),
                ("tool", "read"),
                ("resource", "manual"),
                ("resource_template", "page"),
                ("prompt", "explain"),
            }
            assert directory.complete is True
            found = await runtime.coordinator.mcp_search(turn.session_id, "搜索 仓库", kind="tool")
            assert found.hits[0].capability.name == "search"
            assert all(hit.capability.name != "write" for hit in found.hits)
            assert found.complete is True
            english = await runtime.coordinator.mcp_search(turn.session_id, "repository files")
            assert english.hits[0].capability.name == "read"
            page = await runtime.coordinator.mcp_search(turn.session_id, "repository", limit=1)
            assert len(page.hits) == 1 and page.next_offset == 1
            next_page = await runtime.coordinator.mcp_search(
                turn.session_id, "repository", offset=1, limit=1
            )
            assert len(next_page.hits) == 1 and next_page.next_offset is None
            assert page.hits[0].capability.name != next_page.hits[0].capability.name
            untranslated = await runtime.coordinator.mcp_search(turn.session_id, "查询工单")
            assert untranslated.hits == () and untranslated.complete
            rewritten = await runtime.coordinator.mcp_search(turn.session_id, "read")
            assert rewritten.hits[0].capability.name == "read"
            empty = await runtime.coordinator.mcp_search(
                turn.session_id, "nonexistent_keyword", kind="tool"
            )
            assert empty.hits == () and empty.complete is True
            assert empty.suggestion is not None
            first_generation = directory.servers[0].generation
            await runtime.coordinator.send("continue", session_id=turn.session_id)
            repeated = await runtime.coordinator.mcp_list(turn.session_id)
            assert repeated.servers[0].generation == first_generation
            refreshed = await runtime.coordinator.mcp_list(turn.session_id, refresh=True)
            assert refreshed.servers[0].generation != first_generation
            paths.user_config_file.write_text(
                paths.user_config_file.read_text(encoding="utf-8").replace(
                    'tools = ["search", "read"]', 'tools = ["read"]'
                ),
                encoding="utf-8",
            )
            narrowed = await runtime.coordinator.mcp_list(turn.session_id)
            assert {entry.name for entry in narrowed.entries if entry.kind == "tool"} == {"read"}
            assert narrowed.servers[0].generation != refreshed.servers[0].generation
            await runtime.coordinator.release_session(turn.session_id)
            assert runtime.coordinator.mcp_status(turn.session_id)[0].connection == "not_connected"
            reopened = await runtime.coordinator.mcp_list(turn.session_id)
            assert reopened.servers[0].generation != first_generation
        finally:
            await runtime.aclose()

    asyncio.run(scenario())


def test_capacity_evicts_whole_servers_and_filtered_search_reloads_the_missing_scope(
    tmp_path: Path,
) -> None:
    paths = DataPaths.for_workspace(
        tmp_path, config_root=tmp_path / "config", data_root=tmp_path / "data"
    )
    paths.user_config_file.parent.mkdir(parents=True)
    fixture = Path(__file__).parent / "fixtures" / "mcp_stdio_server.py"
    definitions = "[memory]\nauto_consolidation = false\n"
    for name in ("first", "second", "oversized"):
        definitions += (
            f'[mcp.servers.{name}]\nenabled = true\ntransport = "stdio"\n'
            f"command = {json.dumps(sys.executable)}\n"
            f"args = {json.dumps([str(fixture), '2026-07-28', 'catalog'])}\n"
            'tools = ["search", "read"]\n'
        )
        if name == "oversized":
            definitions += "resources = true\nprompts = true\n"
    paths.user_config_file.write_text(definitions, encoding="utf-8")
    config = resolve_configuration(
        tmp_path, CliOverrides(provider="fake"), data_paths=paths, environment={}
    )

    async def scenario() -> None:
        runtime = build_runtime(
            config,
            FakeApproval(ApprovalDecision.APPROVE),
            runtime_options=RuntimeOptions(
                mcp_directory_limits=MCPDirectoryLimits(entries=2),
            ),
        )
        try:
            turn = await runtime.coordinator.send("hello")
            directory = await runtime.coordinator.mcp_list(turn.session_id)
            states = {server.server_id: server for server in directory.servers}
            assert states["first"].directory == "evicted"
            assert states["second"].directory == "ready"
            assert states["oversized"].reason == "directory_capacity"
            assert directory.complete is False
            assert len(directory.entries) == 2
            assert {entry.server_id for entry in directory.entries} == {"second"}
            found = await runtime.coordinator.mcp_search(turn.session_id, "read", server_id="first")
            assert found.complete is True
            assert len(found.hits) == 1 and found.hits[0].capability.server_id == "first"
            status = {
                server.server_id: server.directory
                for server in runtime.coordinator.mcp_status(turn.session_id)
            }
            assert status == {"first": "ready", "second": "evicted", "oversized": "failed"}
        finally:
            await runtime.aclose()

    asyncio.run(scenario())


def test_invalid_tool_schemas_are_isolated_without_losing_the_other_capabilities(
    tmp_path: Path,
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
        f"args = {json.dumps([str(fixture), '2026-07-28', 'catalog_invalid'])}\n"
        'tools = ["search", "read", "broken", "external_ref", "output_external", "ambiguous"]\n'
        "resources = true\n",
        encoding="utf-8",
    )
    config = resolve_configuration(
        tmp_path, CliOverrides(provider="fake"), data_paths=paths, environment={}
    )

    async def scenario() -> None:
        runtime = build_runtime(config, FakeApproval(ApprovalDecision.APPROVE))
        try:
            turn = await runtime.coordinator.send("hello")
            directory = await runtime.coordinator.mcp_list(turn.session_id)
            assert directory.complete is True
            assert {entry.name for entry in directory.entries} == {
                "search",
                "read",
                "manual",
                "page",
            }
            assert {entry.name for entry in directory.servers[0].rejected} == {
                "broken",
                "external_ref",
                "output_external",
                "ambiguous",
            }
            assert {entry.reason for entry in directory.servers[0].rejected} == {
                "unsupported_input_schema",
                "unsupported_output_schema",
                "duplicate_name",
            }
        finally:
            await runtime.aclose()

    asyncio.run(scenario())


@pytest.mark.parametrize("limit", ["metadata", "index", "pages"])
def test_byte_index_and_page_limits_never_claim_complete_coverage(
    tmp_path: Path, limit: str
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
        f"args = {json.dumps([str(fixture), '2026-07-28', 'catalog'])}\n"
        'tools = ["search", "read"]\n',
        encoding="utf-8",
    )
    config = resolve_configuration(
        tmp_path, CliOverrides(provider="fake"), data_paths=paths, environment={}
    )
    limits = (
        MCPDirectoryLimits(metadata_bytes=1)
        if limit == "metadata"
        else (
            MCPDirectoryLimits(index_bytes=1)
            if limit == "index"
            else MCPDirectoryLimits(pages_per_kind=1)
        )
    )

    async def scenario() -> None:
        runtime = build_runtime(
            config,
            FakeApproval(ApprovalDecision.APPROVE),
            runtime_options=RuntimeOptions(mcp_directory_limits=limits),
        )
        try:
            turn = await runtime.coordinator.send("hello")
            found = await runtime.coordinator.mcp_search(turn.session_id, "read")
            assert found.complete is False
            assert found.hits == ()
            if limit == "pages":
                assert found.servers[0].directory == "partial"
            else:
                assert found.servers[0].directory == "failed"
                assert found.servers[0].reason == "directory_capacity"
        finally:
            await runtime.aclose()

    asyncio.run(scenario())


def test_prompt_only_server_remains_available_when_another_server_cannot_start(
    tmp_path: Path,
) -> None:
    paths = DataPaths.for_workspace(
        tmp_path, config_root=tmp_path / "config", data_root=tmp_path / "data"
    )
    paths.user_config_file.parent.mkdir(parents=True)
    fixture = Path(__file__).parent / "fixtures" / "mcp_stdio_server.py"
    paths.user_config_file.write_text(
        "[memory]\nauto_consolidation = false\n"
        '[mcp.servers.broken]\nenabled = true\ntransport = "stdio"\n'
        'command = "no-such-mcp-server"\n'
        '[mcp.servers.templates]\nenabled = true\ntransport = "stdio"\n'
        f"command = {json.dumps(sys.executable)}\n"
        f"args = {json.dumps([str(fixture), '2025-11-25', 'prompts_only'])}\n"
        'tools = ["search"]\nresources = true\nprompts = true\n'
        '[mcp.servers.disabled]\ntransport = "stdio"\ncommand = "do-not-launch"\n',
        encoding="utf-8",
    )
    config = resolve_configuration(
        tmp_path, CliOverrides(provider="fake"), data_paths=paths, environment={}
    )

    async def scenario() -> None:
        runtime = build_runtime(config, FakeApproval(ApprovalDecision.APPROVE))
        try:
            turn = await runtime.coordinator.send("hello")
            directory = await runtime.coordinator.mcp_list(turn.session_id)
            assert {(entry.server_id, entry.kind, entry.name) for entry in directory.entries} == {
                ("templates", "prompt", "explain")
            }
            assert directory.complete is False
            status = {server.server_id: server for server in directory.servers}
            assert status["broken"].directory == "failed"
            assert status["templates"].directory == "ready"
            assert status["templates"].protocol_version == "2025-11-25"
            assert status["disabled"].connection == "not_connected"
            found = await runtime.coordinator.mcp_search(
                turn.session_id, "explain", kind="prompt", server_id="templates"
            )
            assert found.complete and len(found.hits) == 1
        finally:
            await runtime.aclose()

    asyncio.run(scenario())
