"""Composition root for selecting adapters and wiring runtime dependencies."""

import os
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from jdagent.adapters.context_objects import FileContextObjects
from jdagent.adapters.deepseek import DeepSeekModelPort
from jdagent.adapters.fake import FakeModelPort
from jdagent.adapters.jsonl_session import JsonlSession
from jdagent.adapters.memory import InMemoryContextObjects
from jdagent.application.coordinator import TurnCoordinator
from jdagent.configuration import ResolvedConfiguration, resolve_configuration
from jdagent.context import ContextBuilder
from jdagent.context_archive import reconstruct_request
from jdagent.context_budget import ContextBudget, InputEstimator
from jdagent.context_runtime import ContextService
from jdagent.core.loop import AgentLoop, LoopLimits, ModelEventObserver
from jdagent.data_paths import workspace_identity
from jdagent.domain.json import JsonObject
from jdagent.domain.model import (
    ModelRequest,
    ModelSettings,
    ResponseCompleted,
    SystemPart,
    TextDelta,
)
from jdagent.domain.tools import PermissionDecision, ToolCapability
from jdagent.instructions import TurnInstructions
from jdagent.knowledge.context_references import KnowledgeReferences
from jdagent.knowledge.embedding import OpenAICompatibleEmbedding
from jdagent.maintenance import MemoryMaintenance, PrioritizedModel
from jdagent.memory_service import MemoryService
from jdagent.memory_store import MemoryStore
from jdagent.model_profiles import model_window
from jdagent.ports import (
    ApprovalPort,
    ContextObjects,
    EventObserver,
    ModelPort,
    RuntimeJournal,
    SessionPort,
)
from jdagent.tools.builtins import create_calculator_tool
from jdagent.tools.command_output import CommandLogs
from jdagent.tools.commands import create_command_tools
from jdagent.tools.files import create_file_tools
from jdagent.tools.permissions import SessionPermissionPolicy, active_session_rules
from jdagent.tools.runtime import ToolRegistry, ToolRuntime

DEVELOPMENT_DEEPSEEK_API_KEY_FILE = (
    Path(__file__).resolve().parents[3] / "tmp" / "keys" / "deepseek-api-key.txt"
)


def _empty_json_object() -> JsonObject:
    return {}


def _empty_contexts() -> dict[str, ContextService]:
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

    max_model_calls: int = 16
    max_tool_calls: int = 32
    temperature: float | None = None
    include_tools: bool = True
    allow_local_index: bool = False
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
    maintenance: MemoryMaintenance | None = None

    async def reconstruct_request(self, digest: str) -> ModelRequest:
        if self.maintenance is None or self.maintenance.memory.knowledge_references is None:
            raise ValueError("Request reconstruction dependencies are unavailable")
        return await reconstruct_request(
            digest,
            self.maintenance.objects,
            resolve_knowledge=self.maintenance.memory.knowledge_references.render,
        )

    async def aclose(self) -> None:
        if self.maintenance is not None:
            await self.maintenance.close()
        await self.coordinator.aclose()
        if isinstance(self.model, DeepSeekModelPort):
            await self.model.aclose()


@dataclass(slots=True)
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
    command_permission: PermissionDecision = PermissionDecision.ASK
    capability: ToolCapability = ToolCapability.EXECUTE
    context_budget: ContextBudget | None = None
    session: SessionPort | None = None
    memory: MemoryService | None = None
    objects: ContextObjects = field(default_factory=InMemoryContextObjects)
    estimator: InputEstimator = field(default_factory=InputEstimator)
    contexts: dict[str, ContextService] = field(default_factory=_empty_contexts)
    global_instructions: Path | None = None
    reload_configuration: Callable[[], ResolvedConfiguration] | None = None
    instructions_max_tokens: int | None = None
    initial_base_url: str | None = None
    runtime_limits: LoopLimits | None = None
    maintenance: MemoryMaintenance | None = None

    def prepare_turn(self) -> None:
        if self.reload_configuration is None:
            return
        config = self.reload_configuration()
        if config.provider != self.provider_name:
            raise ValueError("Provider changed; restart before beginning the next Turn")
        if self.initial_base_url is not None and config.base_url != self.initial_base_url:
            raise ValueError("Provider base_url changed; restart before beginning the next Turn")
        if self.memory is not None and config.memory_embedding != self.memory.profile:
            raise ValueError("Memory embedding profile changed; restart to rebuild the index")
        budget = ContextBudget(
            model_window(
                config.provider, config.model, config.base_url, config.model_context_window
            ),
            config.max_context_tokens,
            config.max_output_tokens,
            config.context_safety_tokens,
            config.provider_input_limit,
            config.compact_trigger_ratio,
            config.compact_target_ratio,
        )
        self.model_name = config.model
        self.model_settings = ModelSettings(
            timeout_seconds=config.model_timeout_seconds,
            temperature=self.model_settings.temperature,
            max_output_tokens=config.max_output_tokens,
        )
        self.max_context_tokens = config.max_context_tokens
        self.context_budget = budget
        self.tool_timeout_seconds = config.tool_timeout_seconds
        self.write_permission = PermissionDecision(config.write_permission)
        self.command_permission = PermissionDecision(config.command_permission)
        self.capability = ToolCapability(config.tool_scope)
        self.limits = self.runtime_limits or LoopLimits(
            config.max_model_calls, config.max_tool_calls, config.max_failure_recoveries
        )
        self.global_instructions = config.global_instructions
        self.instructions_max_tokens = config.instructions_max_tokens
        if self.maintenance is not None:
            self.maintenance.enabled = config.memory_auto_consolidation
            self.maintenance.budget = budget
            self.maintenance.model_name = config.model
        if self.memory is not None:
            self.memory.accounting.total_input = config.total_input_token_limit
            self.memory.accounting.total_output = config.total_output_token_limit
            self.memory.accounting.maintenance_input = config.memory_input_token_limit
            self.memory.accounting.maintenance_output = config.memory_output_token_limit

    def context_for(self, session_id: str) -> ContextService | None:
        return self.contexts.get(session_id)

    def create(self, journal: RuntimeJournal) -> AgentLoop:
        if not journal.events:
            raise ValueError("Session journal must contain session_started before loop creation")
        session_id = journal.events[0].session_id
        instructions = TurnInstructions(self.workspace, global_file=self.global_instructions)
        tools = ToolRuntime(
            self.registry,
            SessionPermissionPolicy(
                workspace=self.workspace,
                session_id=session_id,
                rules=active_session_rules(tuple(journal.events)),
                write_ceiling=self.write_permission,
                command_ceiling=self.command_permission,
            ),
            self.approval,
            timeout_seconds=self.tool_timeout_seconds,
            recorder=journal,
            capability=self.capability,
            instructions=instructions,
        )
        context_builder = ContextBuilder(
            model=self.model_name,
            system_parts=self.system_parts,
            tools=self.registry.definitions(self.capability),
            settings=self.model_settings,
            max_context_tokens=self.max_context_tokens,
            provider_options=self.provider_options,
            sliding_window=False,
        )
        context = ContextService(
            builder=context_builder,
            model=self.model,
            journal=journal,
            objects=self.objects,
            budget=self.context_budget
            or ContextBudget(
                self.model.capabilities.context_window,
                self.max_context_tokens,
                self.model_settings.max_output_tokens or 0,
            ),
            estimator=self.estimator,
            provider=self.provider_name,
            instructions=instructions,
            session=self.session,
            memory=self.memory,
            instructions_max_tokens=self.instructions_max_tokens,
        )
        self.contexts[session_id] = context
        return AgentLoop(
            self.model,
            context_builder,
            tools,
            journal,
            limits=self.limits,
            provider_name=self.provider_name,
            model_name=self.model_name,
            model_event_observers=self.model_event_observers,
            context_service=context,
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
    context_budget = ContextBudget(
        model_window(
            configuration.provider,
            configuration.model,
            configuration.base_url,
            configuration.model_context_window,
        ),
        configuration.max_context_tokens,
        configuration.max_output_tokens,
        configuration.context_safety_tokens,
        configuration.provider_input_limit,
        configuration.compact_trigger_ratio,
        configuration.compact_target_ratio,
    )
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
    memory_path = (
        configuration.data_paths.project_directory.parent.parent / "memory-v5.sqlite"
        if configuration.session_directory == configuration.data_paths.sessions_directory
        else configuration.session_directory / "memory-v5.sqlite"
    )

    def reload_configuration() -> ResolvedConfiguration:
        return resolve_configuration(
            workspace, configuration.cli_overrides, data_paths=configuration.data_paths
        )

    def memory_write_allowed() -> bool:
        current = reload_configuration()
        return current.write_permission != "deny" and current.tool_scope != "analyze"

    memory = MemoryService(
        store=MemoryStore(memory_path),
        session=session,
        discovery=session,
        workspace=workspace_identity(workspace),
        workspace_path=workspace,
        write_allowed=memory_write_allowed,
        embedding=OpenAICompatibleEmbedding(
            api_key=os.environ.get("JDAGENT_MEMORY_EMBEDDING_API_KEY")
        )
        if configuration.memory_embedding
        else None,
        profile=configuration.memory_embedding,
        knowledge_references=KnowledgeReferences(
            configuration.data_paths.knowledge_catalog,
            configuration.data_paths.knowledge_backups,
            configuration.data_paths.knowledge_objects,
        ),
    )
    memory.accounting.total_input = configuration.total_input_token_limit
    memory.accounting.total_output = configuration.total_output_token_limit
    tools = (
        (
            create_calculator_tool(),
            *memory.tools(),
            *create_file_tools(workspace),
            *create_command_tools(
                workspace, CommandLogs(configuration.session_directory / "command-output")
            ),
        )
        if options.include_tools
        else ()
    )
    scheduled_model = PrioritizedModel(model)
    objects = FileContextObjects(configuration.session_directory / "objects")
    maintenance = MemoryMaintenance(
        memory,
        scheduled_model,
        configuration.model,
        objects,
        context_budget,
        provider=configuration.provider,
        enabled=configuration.memory_auto_consolidation,
        input_limit=configuration.memory_input_token_limit,
        output_limit=configuration.memory_output_token_limit,
        automatic_allowed=lambda: reload_configuration().memory_auto_consolidation,
    )
    loop_factory = ConfiguredLoopFactory(
        model=scheduled_model,
        model_name=configuration.model,
        provider_name=configuration.provider,
        workspace=workspace,
        registry=ToolRegistry(tools),
        approval=approval,
        system_parts=(SystemPart("You are a careful, concise general-purpose agent."),),
        model_settings=ModelSettings(
            timeout_seconds=configuration.model_timeout_seconds,
            temperature=options.temperature,
            max_output_tokens=configuration.max_output_tokens,
        ),
        max_context_tokens=configuration.max_context_tokens,
        limits=LoopLimits(options.max_model_calls, options.max_tool_calls),
        tool_timeout_seconds=configuration.tool_timeout_seconds,
        provider_options=options.provider_options,
        model_event_observers=model_event_observers,
        write_permission=PermissionDecision(configuration.write_permission),
        command_permission=PermissionDecision(configuration.command_permission),
        capability=ToolCapability(configuration.tool_scope),
        context_budget=context_budget,
        objects=objects,
        session=session,
        memory=memory,
        global_instructions=configuration.global_instructions,
        instructions_max_tokens=configuration.instructions_max_tokens,
        reload_configuration=reload_configuration,
        initial_base_url=configuration.base_url,
        maintenance=maintenance,
        runtime_limits=LoopLimits(options.max_model_calls, options.max_tool_calls)
        if runtime_options is not None
        else None,
    )
    coordinator = TurnCoordinator(
        session=session,
        workspace=workspace,
        loop_factory=loop_factory,
        event_observers=event_observers,
        workspace_identity=workspace_identity(workspace),
        knowledge_catalog=configuration.data_paths.knowledge_catalog,
        knowledge_backups=configuration.data_paths.knowledge_backups,
        knowledge_index=(
            configuration.data_paths.knowledge_directory / "index.sqlite"
            if options.allow_local_index
            else None
        ),
        memory=memory,
    )
    coordinator.after_output = maintenance.start
    return RuntimeComposition(coordinator, model, session, maintenance)
