import asyncio
import sys
import threading
from contextlib import suppress
from pathlib import Path

import pytest

from jdagent.adapters.mcp_connection import SDKMCPConnection
from jdagent.domain.json import require_object
from jdagent.domain.mcp import MCPFailure
from jdagent.mcp_configuration import MCPServerConfiguration
from tests.test_v07_mcp_http import wire_server


@pytest.mark.parametrize("version", ["2026-07-28", "2025-11-25"])
def test_connection_reuses_sdk_session_across_caller_tasks_and_closes(version: str) -> None:
    async def scenario() -> None:
        fixture = Path(__file__).parent / "fixtures" / "mcp_stdio_server.py"
        peer = SDKMCPConnection(
            MCPServerConfiguration(
                server_id="docs",
                transport="stdio",
                enabled=True,
                tools=("read",),
                resources=False,
                prompts=False,
                command=sys.executable,
                args=(str(fixture), version),
                cwd=fixture.parent,
            )
        )
        assert peer.state == "not_connected"
        try:
            first = await asyncio.create_task(peer.request("tools/list", {}))
            second = await asyncio.create_task(peer.request("tools/list", {}))
            assert (
                require_object(first["_meta"], "meta")["fixturePid"]
                == require_object(second["_meta"], "meta")["fixturePid"]
            )
            assert peer.information is not None
            assert peer.information.protocol_version == version
            assert peer.information.tools is True
            assert peer.information.resources is True
            assert peer.information.prompts is True
            assert (await peer.request("resources/list", {}))["resources"] == [
                {"name": "manual", "uri": "docs://manual"}
            ]
            assert (await peer.request("resources/templates/list", {}))["resourceTemplates"] == [
                {"name": "page", "uriTemplate": "docs://{page}"}
            ]
            assert (await peer.request("prompts/list", {}))["prompts"] == [
                {"name": "explain", "arguments": [{"name": "topic", "required": True}]}
            ]
        finally:
            assert await peer.aclose()
        assert peer.state == "closed"

    asyncio.run(scenario())


def test_oversized_catalog_fails_before_returning_any_items_and_closes_connection() -> None:
    async def scenario() -> None:
        fixture = Path(__file__).parent / "fixtures" / "mcp_stdio_server.py"
        peer = SDKMCPConnection(
            MCPServerConfiguration(
                server_id="docs",
                transport="stdio",
                enabled=True,
                tools=(),
                resources=False,
                prompts=False,
                command=sys.executable,
                args=(str(fixture), "2026-07-28"),
                cwd=fixture.parent,
            )
        )
        try:
            with pytest.raises(MCPFailure) as rejected:
                await peer.request("tools/list", {"cursor": "oversized"})
            assert rejected.value.kind == "response_limit"
            assert peer.state == "unavailable"
        finally:
            assert await peer.aclose()

    asyncio.run(scenario())


@pytest.mark.parametrize("version", ["2026-07-28", "2025-11-25"])
def test_http_connection_uses_the_same_negotiated_catalog_boundary(version: str) -> None:
    async def scenario(url: str) -> None:
        peer = SDKMCPConnection(
            MCPServerConfiguration(
                server_id="docs",
                transport="http",
                enabled=True,
                tools=(),
                resources=False,
                prompts=False,
                url=url,
            )
        )
        try:
            result = await peer.request("tools/list", {})
            assert result["tools"] == []
            assert peer.information is not None
            assert peer.information.protocol_version == version
        finally:
            assert await peer.aclose()

    with wire_server(version, "gzip") as url:
        asyncio.run(scenario(url))


@pytest.mark.parametrize("encoding", ["json", "gzip", "sse"])
def test_http_oversize_response_failure_is_sanitized_and_connection_is_discarded(
    encoding: str,
) -> None:
    async def scenario(url: str) -> None:
        peer = SDKMCPConnection(
            MCPServerConfiguration(
                server_id="docs",
                transport="http",
                enabled=True,
                tools=(),
                resources=False,
                prompts=False,
                url=url,
            )
        )
        try:
            with pytest.raises(MCPFailure) as rejected:
                await peer.request("tools/list", {"cursor": "oversized"})
            assert rejected.value.kind == "response_limit"
            assert peer.state == "unavailable"
        finally:
            assert await peer.aclose()

    with wire_server("2026-07-28", encoding) as url:
        asyncio.run(scenario(url))


def test_unsupported_protocol_is_reported_without_poisoning_another_connection() -> None:
    async def scenario() -> None:
        fixture = Path(__file__).parent / "fixtures" / "mcp_stdio_server.py"
        peers = [
            SDKMCPConnection(
                MCPServerConfiguration(
                    server_id=version,
                    transport="stdio",
                    enabled=True,
                    tools=(),
                    resources=False,
                    prompts=False,
                    command=sys.executable,
                    args=(str(fixture), version),
                    cwd=fixture.parent,
                )
            )
            for version in ("2030-01-01", "2026-07-28")
        ]
        try:
            with pytest.raises(MCPFailure) as rejected:
                await peers[0].request("tools/list", {})
            assert rejected.value.kind == "unsupported"
            assert peers[0].state == "unavailable"
            with pytest.raises(MCPFailure):
                await peers[0].request("tools/list", {})
            assert (await peers[1].request("tools/list", {}))["tools"] == []
        finally:
            for peer in peers:
                assert await peer.aclose()

    asyncio.run(scenario())


def test_remote_error_does_not_retry_or_echo_server_text() -> None:
    async def scenario() -> None:
        fixture = Path(__file__).parent / "fixtures" / "mcp_stdio_server.py"
        peer = SDKMCPConnection(
            MCPServerConfiguration(
                server_id="docs",
                transport="stdio",
                enabled=True,
                tools=(),
                resources=False,
                prompts=False,
                command=sys.executable,
                args=(str(fixture), "2026-07-28"),
                cwd=fixture.parent,
            )
        )
        try:
            with pytest.raises(MCPFailure) as rejected:
                await peer.request("tools/list", {"cursor": "remote-error"})
            assert rejected.value.kind == "remote"
            assert rejected.value.rpc_code == -32001
            assert "fixture-secret-error-text" not in str(rejected.value)
            result = await peer.request("tools/list", {})
            assert require_object(result["_meta"], "meta")["listCalls"] == 2
        finally:
            assert await peer.aclose()

    asyncio.run(scenario())


def test_cancelling_a_caller_closes_the_owned_peer_and_does_not_reconnect() -> None:
    async def scenario() -> None:
        fixture = Path(__file__).parent / "fixtures" / "mcp_stdio_server.py"
        peer = SDKMCPConnection(
            MCPServerConfiguration(
                server_id="docs",
                transport="stdio",
                enabled=True,
                tools=(),
                resources=False,
                prompts=False,
                command=sys.executable,
                args=(str(fixture), "2026-07-28"),
                cwd=fixture.parent,
            )
        )
        await peer.request("tools/list", {})
        call = asyncio.create_task(peer.request("tools/list", {"cursor": "hang"}))
        try:
            await asyncio.sleep(0)
            with pytest.raises(MCPFailure) as busy:
                await peer.request("tools/list", {})
            assert busy.value.kind == "busy"
            call.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(call, 6)
            assert peer.state == "closed"
            with pytest.raises(MCPFailure):
                await peer.request("tools/list", {})
        finally:
            call.cancel()
            with suppress(asyncio.CancelledError):
                await call
            assert await peer.aclose()

    asyncio.run(scenario())


def test_close_racing_with_first_connect_also_releases_the_waiting_caller() -> None:
    async def scenario() -> None:
        peer = SDKMCPConnection(
            MCPServerConfiguration(
                server_id="docs",
                transport="http",
                enabled=True,
                tools=(),
                resources=False,
                prompts=False,
                url="http://127.0.0.1:1/",
            )
        )
        call = asyncio.create_task(peer.request("tools/list", {}))
        await asyncio.sleep(0)
        assert await peer.aclose()
        with pytest.raises(MCPFailure):
            await asyncio.wait_for(call, 0.5)

    asyncio.run(scenario())


@pytest.mark.parametrize("value", [None, ""])
def test_missing_credential_is_lazy_and_never_enters_the_transport(
    monkeypatch: pytest.MonkeyPatch, value: str | None
) -> None:
    async def scenario() -> None:
        if value is None:
            monkeypatch.delenv("JDAGENT_TEST_MCP_TOKEN", raising=False)
        else:
            monkeypatch.setenv("JDAGENT_TEST_MCP_TOKEN", value)
        peer = SDKMCPConnection(
            MCPServerConfiguration(
                server_id="docs",
                transport="http",
                enabled=True,
                tools=(),
                resources=False,
                prompts=False,
                url="http://127.0.0.1:1/",
                bearer_env="JDAGENT_TEST_MCP_TOKEN",
            )
        )
        assert peer.state == "not_connected"
        try:
            with pytest.raises(MCPFailure) as rejected:
                await peer.request("tools/list", {})
            assert rejected.value.kind == "configuration"
        finally:
            assert await peer.aclose()

    asyncio.run(scenario())


def test_bearer_reference_is_resolved_once_per_connection(monkeypatch: pytest.MonkeyPatch) -> None:
    async def scenario(url: str) -> None:
        monkeypatch.setenv("JDAGENT_TEST_MCP_TOKEN", "fixture-first-token")
        peer = SDKMCPConnection(
            MCPServerConfiguration(
                server_id="docs",
                transport="http",
                enabled=True,
                tools=(),
                resources=False,
                prompts=False,
                url=url,
                bearer_env="JDAGENT_TEST_MCP_TOKEN",
            )
        )
        try:
            assert (await peer.request("tools/list", {}))["tools"] == []
            monkeypatch.setenv("JDAGENT_TEST_MCP_TOKEN", "fixture-second-token")
            assert (await peer.request("tools/list", {}))["tools"] == []
        finally:
            assert await peer.aclose()

    with wire_server(
        "2026-07-28", "json", expected_authorization="Bearer fixture-first-token"
    ) as url:
        asyncio.run(scenario(url))


@pytest.mark.parametrize("elapsed", [11, 16])
def test_connection_has_one_fifteen_second_budget_without_a_hidden_ten_second_probe(
    monkeypatch: pytest.MonkeyPatch, elapsed: int
) -> None:
    started, release = threading.Event(), threading.Event()

    async def scenario(url: str) -> None:
        peer = SDKMCPConnection(
            MCPServerConfiguration(
                server_id="docs",
                transport="http",
                enabled=True,
                tools=(),
                resources=False,
                prompts=False,
                url=url,
            )
        )
        loop = asyncio.get_running_loop()
        real_time = loop.time
        offset = 0
        with monkeypatch.context() as clock:
            clock.setattr(loop, "time", lambda: real_time() + offset)
            call = asyncio.create_task(peer.request("tools/list", {}))
            try:
                assert await asyncio.to_thread(started.wait, 3)
                offset = elapsed
                await asyncio.sleep(0)
                await asyncio.sleep(0)
                release.set()
                if elapsed > 15:
                    with pytest.raises(MCPFailure) as rejected:
                        await asyncio.wait_for(call, 3)
                    assert rejected.value.kind == "connection_timeout"
                else:
                    assert (await asyncio.wait_for(call, 3))["tools"] == []
            finally:
                release.set()
                call.cancel()
                with suppress(asyncio.CancelledError, MCPFailure):
                    await call
                assert await peer.aclose()

    with wire_server("2026-07-28", "json", discover_gate=(started, release)) as url:
        asyncio.run(scenario(url))
