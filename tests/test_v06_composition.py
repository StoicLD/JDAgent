import asyncio
import json
from pathlib import Path

import httpx
import pytest

import jdagent.composition as composition_module
from jdagent.adapters.fake import FakeApproval, FakeModelPort
from jdagent.adapters.langchain_model import LangChainModelPort
from jdagent.composition import build_runtime
from jdagent.configuration import CliOverrides, ConfigurationError, resolve_configuration
from jdagent.data_paths import DataPaths
from jdagent.domain.errors import StopReason
from jdagent.domain.events import ModelRequestPreparedPayload
from jdagent.domain.tools import ApprovalDecision


def _paths(workspace: Path) -> DataPaths:
    return DataPaths.for_workspace(
        workspace, config_root=workspace / "config", data_root=workspace / "data"
    )


def test_backend_configuration_is_user_scoped_and_cli_overridable(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    paths.user_config_file.parent.mkdir(parents=True)
    paths.user_config_file.write_text('model_adapter = "langchain"\n', encoding="utf-8")
    assert (
        resolve_configuration(tmp_path, data_paths=paths, environment={}).model_adapter
        == "langchain"
    )
    assert (
        resolve_configuration(
            tmp_path, CliOverrides(model_adapter="native"), data_paths=paths, environment={}
        ).model_adapter
        == "native"
    )
    paths.project_config_file.parent.mkdir(parents=True)
    paths.project_config_file.write_text('model_adapter = "native"\n', encoding="utf-8")
    with pytest.raises(ConfigurationError, match="model_adapter"):
        resolve_configuration(tmp_path, data_paths=paths, environment={})


@pytest.mark.parametrize("value", ['"unknown"', "[]", "true", '""'])
def test_invalid_model_adapter_is_rejected_as_configuration_error(
    tmp_path: Path, value: str
) -> None:
    paths = _paths(tmp_path)
    paths.user_config_file.parent.mkdir(parents=True)
    paths.user_config_file.write_text(f"model_adapter = {value}\n", encoding="utf-8")
    with pytest.raises(ConfigurationError, match="model_adapter"):
        resolve_configuration(tmp_path, data_paths=paths, environment={})


def test_fake_provider_does_not_construct_a_network_adapter(tmp_path: Path) -> None:
    config = resolve_configuration(
        tmp_path,
        CliOverrides(provider="fake", model_adapter="langchain"),
        data_paths=_paths(tmp_path),
        environment={},
    )

    async def scenario() -> None:
        runtime = build_runtime(config, FakeApproval(ApprovalDecision.APPROVE))
        try:
            assert isinstance(runtime.model, FakeModelPort)
            turn = await runtime.coordinator.send("hello")
            assert turn.result.stop_reason is StopReason.COMPLETED
        finally:
            await runtime.aclose()

    asyncio.run(scenario())


def test_langchain_runs_through_existing_loop_and_archives_reloaded_settings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = _paths(tmp_path)
    paths.user_config_file.parent.mkdir(parents=True)
    paths.user_config_file.write_text(
        'provider = "deepseek"\nmodel_adapter = "langchain"\nbase_url = "https://test"\n',
        encoding="utf-8",
    )
    paths.project_config_file.parent.mkdir(parents=True)

    def project(model: str, timeout: int, limit: int) -> None:
        paths.project_config_file.write_text(
            f'model = "{model}"\nmodel_context_window = 12000\nmax_context_tokens = 10000\n'
            f"model_timeout_seconds = {timeout}\nmax_output_tokens = {limit}\n"
            "[memory]\nauto_consolidation = false\n",
            encoding="utf-8",
        )

    project("model-a", 4, 1000)
    config = resolve_configuration(
        tmp_path, data_paths=paths, environment={"DEEPSEEK_API_KEY": "dummy-key"}
    )
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            content=(
                b'data: {"choices":[{"index":0,"delta":{"content":"done"},'
                b'"finish_reason":"stop"}]}\n\ndata: [DONE]\n\n'
            ),
        )

    async def scenario() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:

            def adapter(api_key: str, *, base_url: str) -> LangChainModelPort:
                return LangChainModelPort(api_key, base_url=base_url, client=client)

            monkeypatch.setattr(composition_module, "LangChainModelPort", adapter)
            runtime = build_runtime(config, FakeApproval(ApprovalDecision.APPROVE))
            try:
                assert isinstance(runtime.model, LangChainModelPort)
                first = await runtime.coordinator.send("first")
                project("model-b", 9, 2000)
                second = await runtime.coordinator.send("second", session_id=first.session_id)
                assert first.result.stop_reason is second.result.stop_reason is StopReason.COMPLETED
                events = [event async for event in runtime.session.read(first.session_id)]
                manifests = [
                    event.payload
                    for event in events
                    if isinstance(event.payload, ModelRequestPreparedPayload)
                ]
                assert len(manifests) == 2
                for prepared, model, timeout, maximum in zip(
                    manifests, ("model-a", "model-b"), (4, 9), (1000, 2000), strict=True
                ):
                    archived = await runtime.reconstruct_request(prepared.object_hash)
                    assert archived.model == model
                    assert archived.settings.timeout_seconds == timeout
                    assert archived.settings.max_output_tokens == maximum
                paths.user_config_file.write_text(
                    'provider = "deepseek"\nmodel_adapter = "native"\nbase_url = "https://test"\n',
                    encoding="utf-8",
                )
                with pytest.raises(ValueError, match="adapter changed; restart"):
                    await runtime.coordinator.send("third", session_id=first.session_id)
                assert len(requests) == 2
            finally:
                await runtime.aclose()

    asyncio.run(scenario())
    assert [json.loads(request.content)["model"] for request in requests] == ["model-a", "model-b"]
    assert [request.extensions["timeout"]["read"] for request in requests] == [4, 9]
