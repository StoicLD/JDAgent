"""Ambient LangSmith configuration must not export project requests or graph state."""

import asyncio
from pathlib import Path

import httpx
import pytest
from langsmith import (
    Client,
    tracing_context,  # pyright: ignore[reportUnknownVariableType]
)

from jdagent.domain.errors import StopReason
from jdagent.domain.tools import ToolCall
from tests.runtime_matrix import COMBINATIONS, RuntimeCase, model_response


@pytest.mark.parametrize("combination", COMBINATIONS)
@pytest.mark.parametrize("failure", [False, True])
def test_ambient_tracing_cannot_construct_or_send_to_a_cloud_client(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    combination: tuple[str, str],
    failure: bool,
) -> None:
    for name in ("LANGSMITH_TRACING", "LANGCHAIN_TRACING_V2"):
        monkeypatch.setenv(name, "true")
    monkeypatch.setenv("LANGSMITH_API_KEY", "synthetic-tracing-key")
    monkeypatch.setenv("LANGSMITH_ENDPOINT", "http://127.0.0.1:1")
    unexpected: list[str] = []

    def forbidden(*args: object, **kwargs: object) -> None:
        del args, kwargs
        unexpected.append("cloud_client")
        raise AssertionError("Project runtime must not construct or use a tracing client")

    for name in ("__init__", "create_run", "update_run", "batch_ingest_runs"):
        monkeypatch.setattr(Client, name, forbidden)
    case = RuntimeCase(tmp_path, monkeypatch, combination)
    case.workspace.mkdir()
    (case.workspace / "note.txt").write_text("Synthetic private task input", encoding="utf-8")
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if len(requests) == 1:
            return model_response(calls=(ToolCall("read", "read_file", {"path": "note.txt"}),))
        if failure:
            return httpx.Response(503, json={"error": {"message": "local simulated failure"}})
        return model_response("done")

    async def scenario() -> None:
        # An enabled outer context is stronger than an environment-only test.
        with tracing_context(enabled=True):
            async with case.open(handler) as runtime:
                turn = await runtime.coordinator.send("Read note.txt and answer.")
                assert turn.result.stop_reason is (
                    StopReason.MODEL_ERROR if failure else StopReason.COMPLETED
                )
        assert len(requests) == 2
        assert not unexpected

    asyncio.run(scenario())
