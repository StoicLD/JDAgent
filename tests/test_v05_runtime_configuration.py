import asyncio
from pathlib import Path

from jdagent.adapters.fake import FakeApproval
from jdagent.composition import build_runtime
from jdagent.configuration import CliOverrides, resolve_configuration
from jdagent.data_paths import DataPaths
from jdagent.domain.events import ModelRequestPreparedPayload
from jdagent.domain.tools import ApprovalDecision


def test_runtime_uses_configured_window_and_archives_ordinary_requests(tmp_path: Path) -> None:
    paths = DataPaths.for_workspace(
        tmp_path, config_root=tmp_path / "config", data_root=tmp_path / "data"
    )
    paths.project_config_file.parent.mkdir()
    paths.project_config_file.write_text(
        "model_context_window = 12000\nmax_context_tokens = 10000\n"
        "max_output_tokens = 1000\ncontext_safety_tokens = 500\n",
        encoding="utf-8",
    )
    config = resolve_configuration(
        tmp_path, CliOverrides(provider="fake"), data_paths=paths, environment={}
    )

    async def scenario() -> None:
        runtime = build_runtime(config, FakeApproval(ApprovalDecision.APPROVE))
        try:
            turn = await runtime.coordinator.send("hello")
            snapshot = await runtime.coordinator.context_window(turn.session_id)
            assert snapshot.budget.input_limit == 8500
            events = [event async for event in runtime.session.read(turn.session_id)]
            assert any(isinstance(event.payload, ModelRequestPreparedPayload) for event in events)
        finally:
            await runtime.aclose()

    asyncio.run(scenario())
