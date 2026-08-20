from pathlib import Path

from jdagent.application.coordinator import TurnCoordinator
from jdagent.composition import ConfiguredLoopFactory
from jdagent.core.loop import LoopLimits
from jdagent.domain.model import ModelSettings
from jdagent.domain.tools import ToolDefinition
from jdagent.ports import ApprovalPort, EventObserver, ModelPort, SessionPort
from jdagent.tools.runtime import ToolRegistry


def create_test_coordinator(
    *,
    model: ModelPort,
    session: SessionPort,
    tools: tuple[ToolDefinition, ...],
    approval: ApprovalPort,
    workspace: Path,
    workspace_identity: str | None = None,
    event_observers: tuple[EventObserver, ...] = (),
) -> TurnCoordinator:
    factory = ConfiguredLoopFactory(
        model=model,
        model_name="fake",
        provider_name="fake",
        workspace=workspace,
        registry=ToolRegistry(tools),
        approval=approval,
        system_parts=(),
        model_settings=ModelSettings(),
        max_context_tokens=None,
        limits=LoopLimits(),
        tool_timeout_seconds=10.0,
        provider_options={},
        model_event_observers=(),
    )
    return TurnCoordinator(
        session=session,
        workspace=workspace,
        loop_factory=factory,
        workspace_identity=workspace_identity,
        event_observers=event_observers,
    )
