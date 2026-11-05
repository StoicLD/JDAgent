"""Verify composition-owned provider pools close after maintenance and session cleanup."""

import asyncio
from dataclasses import replace
from pathlib import Path

import httpx
import pytest

from jdagent.adapters.fake import FakeApproval
from jdagent.application.coordinator import TurnCoordinator
from jdagent.composition import build_runtime
from jdagent.configuration import CliOverrides, resolve_configuration
from jdagent.data_paths import DataPaths
from jdagent.domain.errors import StopReason
from jdagent.domain.tools import ApprovalDecision
from jdagent.maintenance import MemoryMaintenance
from tests.runtime_matrix import COMBINATIONS, model_response


@pytest.mark.parametrize("combination", COMBINATIONS)
@pytest.mark.parametrize("failure", [False, True])
def test_owned_clients_close_after_application_resources_even_when_cleanup_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    combination: tuple[str, str],
    failure: bool,
) -> None:
    order: list[str] = []
    async_clients: list[httpx.AsyncClient] = []
    sync_clients: list[httpx.Client] = []
    sent: list[httpx.Request] = []
    close_async = httpx.AsyncClient.aclose
    close_sync = httpx.Client.close
    close_maintenance = MemoryMaintenance.close
    close_coordinator = TurnCoordinator.aclose

    async def send(
        client: httpx.AsyncClient, request: httpx.Request, **kwargs: object
    ) -> httpx.Response:
        del client, kwargs
        sent.append(request)
        response = model_response()
        response.request = request
        return response

    async def async_close(client: httpx.AsyncClient) -> None:
        await close_async(client)
        async_clients.append(client)
        order.append("async_pool")

    def sync_close(client: httpx.Client) -> None:
        close_sync(client)
        sync_clients.append(client)
        order.append("sync_pool")

    async def maintenance_close(worker: MemoryMaintenance) -> None:
        await close_maintenance(worker)
        order.append("maintenance")
        if failure:
            raise RuntimeError("injected cleanup failure")

    async def coordinator_close(coordinator: TurnCoordinator) -> None:
        await close_coordinator(coordinator)
        order.append("coordinator")

    monkeypatch.setattr(httpx.AsyncClient, "send", send)
    monkeypatch.setattr(httpx.AsyncClient, "aclose", async_close)
    monkeypatch.setattr(httpx.Client, "close", sync_close)
    monkeypatch.setattr(MemoryMaintenance, "close", maintenance_close)
    monkeypatch.setattr(TurnCoordinator, "aclose", coordinator_close)
    engine, adapter = combination
    paths = DataPaths.for_workspace(
        tmp_path, config_root=tmp_path / "config", data_root=tmp_path / "data"
    )
    config = resolve_configuration(
        tmp_path,
        CliOverrides(
            provider="deepseek",
            base_url="https://test",
            runtime_engine=engine,
            model_adapter=adapter,
            model_context_window=120000,
        ),
        data_paths=paths,
        environment={"DEEPSEEK_API_KEY": "dummy-key"},
    )
    config = replace(config, memory_auto_consolidation=False)

    async def scenario() -> None:
        # No injected client: exercise the real constructor ownership branches.
        runtime = build_runtime(config, FakeApproval(ApprovalDecision.APPROVE))
        result = await runtime.coordinator.send("hello")
        assert result.result.stop_reason is StopReason.COMPLETED
        if failure:
            with pytest.raises(RuntimeError, match="injected cleanup failure"):
                await runtime.aclose()
        else:
            await runtime.aclose()
        assert order[:2] == ["maintenance", "coordinator"]
        assert order[2:] == (
            ["async_pool", "sync_pool"] if adapter == "langchain" else ["async_pool"]
        )
        assert len(async_clients) == 1 and async_clients[0].is_closed
        assert len(sync_clients) == (1 if adapter == "langchain" else 0)
        assert all(client.is_closed for client in sync_clients)
        assert len(sent) == 1
        # A fresh instance can acquire the same session after cleanup.
        from jdagent.adapters.jsonl_session import JsonlSession

        session = JsonlSession(config.session_directory)
        await session.acquire(result.session_id)
        await session.release(result.session_id)

    asyncio.run(scenario())
