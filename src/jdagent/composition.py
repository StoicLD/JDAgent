"""Composition root for selecting adapters and wiring runtime dependencies."""

from dataclasses import dataclass, field
from pathlib import Path

from jdagent.adapters.deepseek import DeepSeekModelPort
from jdagent.adapters.fake import FakeModelPort
from jdagent.adapters.jsonl_session import JsonlSession
from jdagent.application.coordinator import TurnCoordinator
from jdagent.configuration import ResolvedConfiguration
from jdagent.context import ContextBuilder
from jdagent.core.loop import AgentLoop, LoopLimits, ModelEventObserver
from jdagent.data_paths import workspace_identity
from jdagent.domain.json import JsonObject
from jdagent.domain.model import ModelSettings, ResponseCompleted, SystemPart, TextDelta
from jdagent.domain.tools import PermissionDecision
from jdagent.ports import ApprovalPort, EventObserver, ModelPort, RuntimeJournal
from jdagent.tools.builtins import create_builtin_tools
from jdagent.tools.permissions import SessionPermissionPolicy, active_session_rules
from jdagent.tools.runtime import ToolRegistry, ToolRuntime
from jdagent.tools.workspace import WorkspacePathResolver

DEVELOPMENT_DEEPSEEK_API_KEY_FILE = (
    Path(__file__).resolve().parents[3] / "tmp" / "keys" / "deepseek-api-key.txt"
)


def _empty_json_object() -> JsonObject:
    return {}


def load_deepseek_api_key(
    explicit_key: str | None,
    key_file: Path = DEVELOPMENT_DEEPSEEK_API_KEY_FILE,
) -> str | None:
    """Prefer an explicit key, then read the approved development-only key file."""

    if explicit_key is not None and explicit_key.strip():
        return explicit_key.strip()
    try:
        file_key = key_file.read_text(encoding="utf-8-sig").strip()
    except FileNotFoundError:
        return None
    except OSError as error:
        raise ValueError("DeepSeek API key file could not be read") from error
    if not file_key:
        raise ValueError("DeepSeek API key file must not be empty")
    return file_key


@dataclass(frozen=True, slots=True)
class RuntimeOptions:
    """Loop-only knobs that do not duplicate resolved host configuration."""

    max_model_calls: int = 8
    max_tool_calls: int = 16
    provider_options: JsonObject = field(default_factory=_empty_json_object)

    def __post_init__(self) -> None:
        if self.max_model_calls <= 0:
            raise ValueError("max_model_calls must be positive")
        if self.max_tool_calls <= 0:
            raise ValueError("max_tool_calls must be positive")


@dataclass(slots=True)
class RuntimeComposition:
    """Own the composed coordinator and adapter lifecycle."""

    coordinator: TurnCoordinator
    model: ModelPort
    session: JsonlSession

    async def aclose(self) -> None:
        if isinstance(self.model, DeepSeekModelPort):
            await self.model.aclose()


@dataclass(frozen=True, slots=True)
class ConfiguredLoopFactory:
    """Create per-turn loops from dependencies chosen by the composition root."""

    model: ModelPort
    model_name: str
    provider_name: str
    workspace: Path
    registry: ToolRegistry
    approval: ApprovalPort
    system_parts: tuple[SystemPart, ...]
    model_settings: ModelSettings
    max_context_tokens: int | None
    limits: LoopLimits
    tool_timeout_seconds: float
    provider_options: JsonObject
    model_event_observers: tuple[ModelEventObserver, ...]
    write_permission: PermissionDecision = PermissionDecision.ASK

    def create(self, journal: RuntimeJournal) -> AgentLoop:
        if not journal.events:
            raise ValueError("Session journal must contain session_started before loop creation")
        session_id = journal.events[0].session_id
        tools = ToolRuntime(
            self.registry,
            SessionPermissionPolicy(
                workspace=self.workspace,
                session_id=session_id,
                rules=active_session_rules(tuple(journal.events)),
                write_ceiling=self.write_permission,
            ),
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
    configuration: ResolvedConfiguration,
    approval: ApprovalPort,
    *,
    runtime_options: RuntimeOptions | None = None,
    model_event_observers: tuple[ModelEventObserver, ...] = (),
    event_observers: tuple[EventObserver, ...] = (),
) -> RuntimeComposition:
    """Select concrete adapters and wire them into the application seam."""

    options = runtime_options or RuntimeOptions()
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
        api_key = load_deepseek_api_key(
            configuration.api_key,
            configuration.api_key_file or DEVELOPMENT_DEEPSEEK_API_KEY_FILE,
        )
        if api_key is None:
            raise ValueError(
                "DEEPSEEK_API_KEY or the development key file is required for provider=deepseek"
            )
        model = DeepSeekModelPort(
            api_key,
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
        workspace=workspace,
        registry=ToolRegistry(tools),
        approval=approval,
        system_parts=(SystemPart("You are a careful, concise general-purpose agent."),),
        model_settings=ModelSettings(timeout_seconds=configuration.model_timeout_seconds),
        max_context_tokens=configuration.max_context_tokens,
        limits=LoopLimits(options.max_model_calls, options.max_tool_calls),
        tool_timeout_seconds=configuration.tool_timeout_seconds,
        provider_options=options.provider_options,
        model_event_observers=model_event_observers,
        write_permission=PermissionDecision(configuration.write_permission),
    )
    coordinator = TurnCoordinator(
        session=session,
        workspace=workspace,
        loop_factory=loop_factory,
        event_observers=event_observers,
        workspace_identity=workspace_identity(workspace),
    )
    return RuntimeComposition(coordinator, model, session)
