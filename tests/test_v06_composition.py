import asyncio
import json
from pathlib import Path

import httpx
import pytest

import jdagent.composition as composition_module
from jdagent.adapters.deepseek import DeepSeekModelPort
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


@pytest.mark.parametrize(
    "field, selected", [("model_adapter", "langchain"), ("runtime_engine", "langgraph")]
)
def test_backend_configuration_is_user_scoped_and_cli_overridable(
    tmp_path: Path, field: str, selected: str
) -> None:
    paths = _paths(tmp_path)
    paths.user_config_file.parent.mkdir(parents=True)
    paths.user_config_file.write_text(f'{field} = "{selected}"\n', encoding="utf-8")
    resolved = resolve_configuration(tmp_path, data_paths=paths, environment={})
    assert getattr(resolved, field) == selected
    overrides = (
        CliOverrides(model_adapter="native")
        if field == "model_adapter"
        else CliOverrides(runtime_engine="native")
    )
    resolved = resolve_configuration(tmp_path, overrides, data_paths=paths, environment={})
    assert getattr(resolved, field) == "native"
    paths.project_config_file.parent.mkdir(parents=True)
    paths.project_config_file.write_text(f'{field} = "native"\n', encoding="utf-8")
    with pytest.raises(ConfigurationError, match=field):
        resolve_configuration(tmp_path, data_paths=paths, environment={})


@pytest.mark.parametrize("field", ["model_adapter", "runtime_engine"])
@pytest.mark.parametrize("value", ['"unknown"', "[]", "true", '""'])
def test_invalid_backend_is_rejected_as_configuration_error(
    tmp_path: Path, value: str, field: str
) -> None:
    paths = _paths(tmp_path)
    paths.user_config_file.parent.mkdir(parents=True)
    paths.user_config_file.write_text(f"{field} = {value}\n", encoding="utf-8")
    with pytest.raises(ConfigurationError, match=field):
        resolve_configuration(tmp_path, data_paths=paths, environment={})


@pytest.mark.parametrize("engine", ["native", "langgraph"])
def test_fake_provider_does_not_construct_a_network_adapter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, engine: str
) -> None:
    _forbid_other_runner(monkeypatch, engine)
    config = resolve_configuration(
        tmp_path,
        CliOverrides(provider="fake", model_adapter="langchain", runtime_engine=engine),
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


@pytest.mark.parametrize("engine", ["native", "langgraph"])
@pytest.mark.parametrize("model_adapter", ["native", "langchain"])
@pytest.mark.parametrize("restart_field", ["model_adapter", "runtime_engine"])
def test_four_combinations_archive_reloaded_settings_and_require_backend_restart(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    engine: str,
    model_adapter: str,
    restart_field: str,
) -> None:
    _forbid_other_runner(monkeypatch, engine)
    paths = _paths(tmp_path)
    paths.user_config_file.parent.mkdir(parents=True)
    paths.user_config_file.write_text(
        f'provider = "deepseek"\nmodel_adapter = "{model_adapter}"\n'
        f'runtime_engine = "{engine}"\nbase_url = "https://test"\n',
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
        async with httpx.AsyncClient(
            base_url="https://test", transport=httpx.MockTransport(handler)
        ) as client:
            adapter_type = LangChainModelPort if model_adapter == "langchain" else DeepSeekModelPort

            def adapter(api_key: str, *, base_url: str) -> LangChainModelPort | DeepSeekModelPort:
                return adapter_type(api_key, base_url=base_url, client=client)

            monkeypatch.setattr(composition_module, adapter_type.__name__, adapter)
            runtime = build_runtime(config, FakeApproval(ApprovalDecision.APPROVE))
            try:
                assert isinstance(runtime.model, adapter_type)
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
                selected_adapter = model_adapter
                selected_engine = engine
                if restart_field == "model_adapter":
                    selected_adapter = "native" if model_adapter == "langchain" else "langchain"
                else:
                    selected_engine = "native" if engine == "langgraph" else "langgraph"
                paths.user_config_file.write_text(
                    f'provider = "deepseek"\nmodel_adapter = "{selected_adapter}"\n'
                    f'runtime_engine = "{selected_engine}"\nbase_url = "https://test"\n',
                    encoding="utf-8",
                )
                with pytest.raises(ValueError, match="changed; restart"):
                    await runtime.coordinator.send("third", session_id=first.session_id)
                assert len(requests) == 2
            finally:
                await runtime.aclose()

    asyncio.run(scenario())
    assert [json.loads(request.content)["model"] for request in requests] == ["model-a", "model-b"]
    assert [request.extensions["timeout"]["read"] for request in requests] == [4, 9]


def _forbid_other_runner(monkeypatch: pytest.MonkeyPatch, engine: str) -> None:
    def unexpected(*args: object, **kwargs: object) -> None:
        del args, kwargs
        raise AssertionError("Composition selected the wrong execution engine")

    monkeypatch.setattr(
        composition_module,
        "AgentLoop" if engine == "langgraph" else "LangGraphTurnRunner",
        unexpected,
    )
