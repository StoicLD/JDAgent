"""Composition root for selecting adapters and wiring runtime dependencies."""

import math
from dataclasses import dataclass, field
from pathlib import Path

from jdagent.adapters.deepseek import DeepSeekModelPort
from jdagent.adapters.fake import FakeModelPort
from jdagent.adapters.jsonl_session import JsonlSession
from jdagent.application.coordinator import TurnCoordinator
from jdagent.context import ContextBuilder
from jdagent.core.loop import AgentLoop, LoopLimits, ModelEventObserver
from jdagent.domain.json import JsonObject
from jdagent.domain.model import ModelSettings, ResponseCompleted, SystemPart, TextDelta
from jdagent.ports import ApprovalPort, EventObserver, ModelPort, RuntimeJournal
from jdagent.tools.builtins import create_builtin_tools
from jdagent.tools.permissions import DefaultPermissionPolicy
from jdagent.tools.runtime import ToolRegistry, ToolRuntime
from jdagent.tools.workspace import WorkspacePathResolver


def _empty_json_object() -> JsonObject:
    return {}


@dataclass(frozen=True, slots=True)
class RuntimeConfiguration:
    """Validated host configuration consumed only by the composition root."""

    provider: str
    model: str
    workspace: Path
    session_directory: Path
    api_key: str | None = None
    base_url: str = "https://api.deepseek.com"
    model_timeout_seconds: float = 30.0
    tool_timeout_seconds: float = 10.0
    max_model_calls: int = 8
    max_tool_calls: int = 16
    max_context_tokens: int | None = None
    fake_delay_seconds: float = 0.0
    provider_options: JsonObject = field(default_factory=_empty_json_object)

    def __post_init__(self) -> None:
        if not self.model.strip():
            raise ValueError("model must be a non-empty string")
        if not math.isfinite(self.model_timeout_seconds) or self.model_timeout_seconds <= 0:
            raise ValueError("model_timeout_seconds must be a positive finite number")
        if not math.isfinite(self.tool_timeout_seconds) or self.tool_timeout_seconds <= 0:
            raise ValueError("tool_timeout_seconds must be a positive finite number")
        if self.max_model_calls <= 0:
            raise ValueError("max_model_calls must be positive")
        if self.max_tool_calls <= 0:
            raise ValueError("max_tool_calls must be positive")
        if self.max_context_tokens is not None and self.max_context_tokens <= 0:
            raise ValueError("max_context_tokens must be positive when provided")
        if not math.isfinite(self.fake_delay_seconds) or self.fake_delay_seconds < 0:
            raise ValueError("fake_delay_seconds must be a non-negative finite number")


@dataclass(slots=True)
class RuntimeComposition:
    """Own the composed coordinator and adapter lifecycle."""

    coordinator: TurnCoordinator
    model: ModelPort

    async def aclose(self) -> None:
        if isinstance(self.model, DeepSeekModelPort):
            await self.model.aclose()


@dataclass(frozen=True, slots=True)
class ConfiguredLoopFactory:
    """Create per-turn loops from dependencies chosen by the composition root."""

    model: ModelPort
    model_name: str
    provider_name: str
    registry: ToolRegistry
    approval: ApprovalPort
    system_parts: tuple[SystemPart, ...]
    model_settings: ModelSettings
    max_context_tokens: int | None
    limits: LoopLimits
    tool_timeout_seconds: float
    provider_options: JsonObject
    model_event_observers: tuple[ModelEventObserver, ...]

    def create(self, journal: RuntimeJournal) -> AgentLoop:
        tools = ToolRuntime(
            self.registry,
            DefaultPermissionPolicy(),
            self.approval,
            timeout_seconds=self.tool_timeout_seconds,
            recorder=journal,
        )
        context_builder = ContextBuilder(
            model=self.model_name,
            system_parts=self.system_parts,
            tools=self.registry.definitions(),
            settings=self.model_settings,
            max_context_tokens=self.max_context_tokens,
            provider_options=self.provider_options,
        )
        return AgentLoop(
            self.model,
            context_builder,
            tools,
            journal,
            limits=self.limits,
            provider_name=self.provider_name,
            model_name=self.model_name,
            model_event_observers=self.model_event_observers,
        )


def build_runtime(
    configuration: RuntimeConfiguration,
    approval: ApprovalPort,
    *,
    model_event_observers: tuple[ModelEventObserver, ...] = (),
    event_observers: tuple[EventObserver, ...] = (),
) -> RuntimeComposition:
    """Select concrete adapters and wire them into the application seam."""

    workspace = configuration.workspace.resolve(strict=True)
    resolver = WorkspacePathResolver(workspace)
    if configuration.provider == "fake":
        model: ModelPort = FakeModelPort(
            scripts=(
                (
                    TextDelta("Offline fake model response."),
                    ResponseCompleted("stop"),
                ),
            ),
            repeat_last=True,
            delay_seconds=configuration.fake_delay_seconds,
        )
    elif configuration.provider == "deepseek":
        if not configuration.api_key:
            raise ValueError("DEEPSEEK_API_KEY is required for provider=deepseek")
        model = DeepSeekModelPort(
            configuration.api_key,
            base_url=configuration.base_url,
        )
    else:
        raise ValueError(f"Unsupported provider: {configuration.provider}")

    session = JsonlSession(configuration.session_directory)
    tools = create_builtin_tools(resolver)
    loop_factory = ConfiguredLoopFactory(
        model=model,
        model_name=configuration.model,
        provider_name=configuration.provider,
        registry=ToolRegistry(tools),
        approval=approval,
        system_parts=(SystemPart("You are a careful, concise general-purpose agent."),),
        model_settings=ModelSettings(timeout_seconds=configuration.model_timeout_seconds),
        max_context_tokens=configuration.max_context_tokens,
        limits=LoopLimits(configuration.max_model_calls, configuration.max_tool_calls),
        tool_timeout_seconds=configuration.tool_timeout_seconds,
        provider_options=configuration.provider_options,
        model_event_observers=model_event_observers,
    )
    coordinator = TurnCoordinator(
        session=session,
        workspace=workspace,
        loop_factory=loop_factory,
        event_observers=event_observers,
    )
    return RuntimeComposition(coordinator, model)
