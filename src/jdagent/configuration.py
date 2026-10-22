"""Layered CLI configuration resolved before runtime composition."""

import math
import os
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import cast

from jdagent.data_paths import DataPaths
from jdagent.knowledge.types import EmbeddingProfile

_CONTEXT_FIELDS = frozenset(
    {
        "model_context_window",
        "max_output_tokens",
        "context_safety_tokens",
        "provider_input_limit",
        "compact_trigger_ratio",
        "compact_target_ratio",
        "global_instructions",
        "instructions_max_tokens",
        "max_model_calls",
        "max_tool_calls",
        "max_failure_recoveries",
        "memory",
    }
)
_USER_FIELDS = _CONTEXT_FIELDS | frozenset(
    {
        "provider",
        "model",
        "base_url",
        "api_key_file",
        "model_timeout_seconds",
        "tool_timeout_seconds",
        "max_context_tokens",
        "fake_delay_seconds",
        "write_permission",
        "command_permission",
        "tool_scope",
    }
)
_PROJECT_FIELDS = _CONTEXT_FIELDS | frozenset(
    {
        "model",
        "model_timeout_seconds",
        "tool_timeout_seconds",
        "max_context_tokens",
        "fake_delay_seconds",
        "write_permission",
        "command_permission",
        "tool_scope",
    }
)


class ConfigurationError(ValueError):
    """A safe, actionable configuration failure."""


@dataclass(frozen=True, slots=True)
class CliOverrides:
    """Only explicitly supplied CLI values; absent fields do not override files."""

    provider: str | None = None
    model: str | None = None
    base_url: str | None = None
    model_timeout_seconds: float | None = None
    tool_timeout_seconds: float | None = None
    max_context_tokens: int | None = None
    fake_delay_seconds: float | None = None
    write_permission: str | None = None
    data_dir: Path | None = None
    command_permission: str | None = None
    tool_scope: str | None = None
    model_context_window: int | None = None
    max_output_tokens: int | None = None


@dataclass(frozen=True, slots=True)
class ResolvedConfiguration:
    """Validated immutable values consumed by the composition root."""

    provider: str
    model: str
    base_url: str
    model_timeout_seconds: float
    tool_timeout_seconds: float
    max_context_tokens: int | None
    fake_delay_seconds: float
    write_permission: str
    api_key: str | None = field(repr=False)
    api_key_file: Path | None
    workspace: Path
    session_directory: Path
    data_paths: DataPaths
    command_permission: str = "ask"
    tool_scope: str = "execute"
    model_context_window: int | None = None
    max_output_tokens: int = 4096
    context_safety_tokens: int | None = None
    provider_input_limit: int | None = None
    compact_trigger_ratio: float = 0.9
    compact_target_ratio: float = 0.4
    global_instructions: Path | None = None
    instructions_max_tokens: int | None = None
    max_model_calls: int = 16
    max_tool_calls: int = 32
    max_failure_recoveries: int = 2
    memory_auto_consolidation: bool = True
    memory_headless_drain_seconds: float = 120
    memory_input_token_limit: int = 10_000_000
    memory_output_token_limit: int = 1_000_000
    total_input_token_limit: int = 30_000_000
    total_output_token_limit: int = 3_000_000
    memory_embedding: EmbeddingProfile | None = None
    cli_overrides: CliOverrides = field(default_factory=CliOverrides, repr=False)


def _read_layer(path: Path, *, layer: str, allowed: frozenset[str]) -> dict[str, object]:
    if not path.exists():
        return {}
    try:
        with path.open("rb") as stream:
            loaded = tomllib.load(stream)
    except (OSError, tomllib.TOMLDecodeError) as error:
        raise ConfigurationError(f"Invalid {layer} configuration at {path}: {error}") from error
    unknown = sorted(set(loaded) - allowed)
    if unknown:
        names = ", ".join(unknown)
        raise ConfigurationError(
            f"{layer.capitalize()} configuration at {path} contains forbidden or unknown "
            f"field(s): {names}"
        )
    return cast(dict[str, object], loaded)


def _non_empty_string(values: Mapping[str, object], name: str) -> str:
    value = values[name]
    if not isinstance(value, str) or not value.strip():
        raise ConfigurationError(f"Configuration field {name} must be a non-empty string")
    return value.strip()


def _positive_number(values: Mapping[str, object], name: str) -> float:
    value = values[name]
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigurationError(f"Configuration field {name} must be a number")
    number = float(value)
    if not math.isfinite(number) or number <= 0:
        raise ConfigurationError(f"Configuration field {name} must be positive and finite")
    return number


def _validate(values: dict[str, object]) -> None:
    for name in (
        "model_context_window",
        "max_output_tokens",
        "provider_input_limit",
        "context_safety_tokens",
        "instructions_max_tokens",
        "max_model_calls",
        "max_tool_calls",
        "max_failure_recoveries",
    ):
        value = values[name]
        minimum = 0 if name == "context_safety_tokens" else 1
        if value is not None and (
            isinstance(value, bool) or not isinstance(value, int) or value < minimum
        ):
            raise ConfigurationError(f"{name} must be an integer >= {minimum}")
    trigger = _positive_number(values, "compact_trigger_ratio")
    target = _positive_number(values, "compact_target_ratio")
    if not target < trigger < 1:
        raise ConfigurationError("Context ratios must satisfy 0 < target < trigger < 1")
    provider = _non_empty_string(values, "provider")
    values["provider"] = provider
    if provider not in {"fake", "deepseek"}:
        raise ConfigurationError("Configuration field provider must be fake or deepseek")
    values["model"] = _non_empty_string(values, "model")
    values["base_url"] = _non_empty_string(values, "base_url")
    _positive_number(values, "model_timeout_seconds")
    _positive_number(values, "tool_timeout_seconds")
    max_context = values["max_context_tokens"]
    if max_context is not None and (
        isinstance(max_context, bool) or not isinstance(max_context, int) or max_context <= 0
    ):
        raise ConfigurationError(
            "Configuration field max_context_tokens must be a positive integer"
        )
    fake_delay = values["fake_delay_seconds"]
    if isinstance(fake_delay, bool) or not isinstance(fake_delay, (int, float)):
        raise ConfigurationError("Configuration field fake_delay_seconds must be a number")
    if not math.isfinite(float(fake_delay)) or float(fake_delay) < 0:
        raise ConfigurationError(
            "Configuration field fake_delay_seconds must be non-negative and finite"
        )
    if provider != "fake" and float(fake_delay) != 0:
        raise ConfigurationError("fake_delay_seconds is only valid for provider=fake")
    permission = _non_empty_string(values, "write_permission")
    values["write_permission"] = permission
    if permission not in {"ask", "deny"}:
        raise ConfigurationError("Configuration field write_permission must be ask or deny")
    if values["command_permission"] not in {"ask", "deny"}:
        raise ConfigurationError("command_permission must be ask or deny")
    if values["tool_scope"] not in {"analyze", "edit", "execute"}:
        raise ConfigurationError("tool_scope must be analyze, edit or execute")


def _apply_layer(values: dict[str, object], layer: Mapping[str, object]) -> None:
    for name, value in layer.items():
        values[name] = value


def resolve_configuration(
    workspace: Path,
    cli_overrides: CliOverrides | None = None,
    *,
    data_paths: DataPaths | None = None,
    environment: Mapping[str, str] = os.environ,
) -> ResolvedConfiguration:
    """Resolve CLI > project > user > defaults and validate the complete result."""

    overrides = cli_overrides or CliOverrides()
    paths = data_paths or DataPaths.for_workspace(workspace, environment=environment)
    values: dict[str, object] = {
        "provider": "deepseek",
        "model": "deepseek-v4-flash",
        "base_url": "https://api.deepseek.com",
        "model_timeout_seconds": 30.0,
        "tool_timeout_seconds": 10.0,
        "max_context_tokens": 64000,
        "fake_delay_seconds": 0.0,
        "write_permission": "ask",
        "command_permission": "ask",
        "tool_scope": "execute",
        "model_context_window": None,
        "max_output_tokens": 4096,
        "context_safety_tokens": None,
        "provider_input_limit": None,
        "compact_trigger_ratio": 0.9,
        "compact_target_ratio": 0.4,
        "global_instructions": None,
        "instructions_max_tokens": None,
        "max_model_calls": 16,
        "max_tool_calls": 32,
        "max_failure_recoveries": 2,
        "memory": {},
    }
    user = _read_layer(paths.user_config_file, layer="user", allowed=_USER_FIELDS)
    project = _read_layer(paths.project_config_file, layer="project", allowed=_PROJECT_FIELDS)
    api_key_file_value = user.pop("api_key_file", None)
    user_permission = (
        _non_empty_string(user, "write_permission") if "write_permission" in user else None
    )
    project_permission = (
        _non_empty_string(project, "write_permission") if "write_permission" in project else None
    )
    if user_permission == "deny" and project_permission == "ask":
        raise ConfigurationError(
            "Project configuration cannot relax user write_permission from deny to ask"
        )
    if user.get("command_permission") == "deny" and project.get("command_permission") == "ask":
        raise ConfigurationError("Project cannot relax user command_permission")
    _apply_layer(values, user)
    _apply_layer(values, project)
    for item in fields(overrides):
        if item.name == "data_dir":
            continue
        value = getattr(overrides, item.name)
        if value is not None:
            values[item.name] = value
    _validate(values)
    for permission_name in ("write_permission", "command_permission"):
        if user.get(permission_name) == "deny" or project.get(permission_name) == "deny":
            values[permission_name] = "deny"

    api_key_file: Path | None = None
    if api_key_file_value is not None:
        if not isinstance(api_key_file_value, str) or not api_key_file_value.strip():
            raise ConfigurationError(
                "User configuration field api_key_file must be a non-empty path"
            )
        candidate = Path(api_key_file_value).expanduser()
        api_key_file = (
            candidate if candidate.is_absolute() else paths.user_config_file.parent / candidate
        )

    memory = values["memory"]
    if not isinstance(memory, dict):
        raise ConfigurationError("memory must be a TOML table")
    memory_values = cast(dict[str, object], memory)
    if set(memory_values) - {
        "auto_consolidation",
        "headless_drain_seconds",
        "input_token_limit",
        "output_token_limit",
        "total_input_token_limit",
        "total_output_token_limit",
        "embedding",
    }:
        raise ConfigurationError("Unknown memory configuration field")
    auto = memory_values.get("auto_consolidation", True)
    drain = memory_values.get("headless_drain_seconds", 120)
    input_limit = memory_values.get("input_token_limit", 10_000_000)
    output_limit = memory_values.get("output_token_limit", 1_000_000)
    total_input = memory_values.get("total_input_token_limit", 30_000_000)
    total_output = memory_values.get("total_output_token_limit", 3_000_000)
    if not isinstance(auto, bool):
        raise ConfigurationError("memory.auto_consolidation must be boolean")
    if (
        isinstance(drain, bool)
        or not isinstance(drain, (int, float))
        or not math.isfinite(drain)
        or drain < 0
    ):
        raise ConfigurationError("memory.headless_drain_seconds must be finite and nonnegative")
    if any(
        isinstance(value, bool) or not isinstance(value, int) or value <= 0
        for value in (input_limit, output_limit, total_input, total_output)
    ):
        raise ConfigurationError("Memory token limits must be positive integers")
    embedding: EmbeddingProfile | None = None
    raw_embedding = memory_values.get("embedding")
    if raw_embedding is not None:
        if not isinstance(raw_embedding, dict):
            raise ConfigurationError("memory.embedding must be a table")
        raw_profile = cast(dict[str, object], raw_embedding)
        if (
            set(raw_profile) != {"base_url", "model", "dimension"}
            or not isinstance(raw_profile.get("dimension"), int)
            or isinstance(raw_profile["dimension"], bool)
        ):
            raise ConfigurationError(
                "memory.embedding requires base_url, model and integer dimension"
            )
        dimension = cast(int, raw_profile["dimension"])
        if dimension <= 0:
            raise ConfigurationError("Embedding dimension must be positive")
        embedding = EmbeddingProfile(
            base_url=_non_empty_string(raw_profile, "base_url"),
            model=_non_empty_string(raw_profile, "model"),
            dimension=dimension,
        )
    global_value = values["global_instructions"]
    if global_value is not None and (not isinstance(global_value, str) or not global_value.strip()):
        raise ConfigurationError("global_instructions must be an explicit path")
    return ResolvedConfiguration(
        provider=cast(str, values["provider"]),
        model=cast(str, values["model"]),
        base_url=cast(str, values["base_url"]),
        model_timeout_seconds=float(cast(float, values["model_timeout_seconds"])),
        tool_timeout_seconds=float(cast(float, values["tool_timeout_seconds"])),
        max_context_tokens=cast(int | None, values["max_context_tokens"]),
        fake_delay_seconds=float(cast(float, values["fake_delay_seconds"])),
        write_permission=cast(str, values["write_permission"]),
        api_key=environment.get("DEEPSEEK_API_KEY"),
        api_key_file=api_key_file,
        workspace=paths.workspace,
        session_directory=overrides.data_dir or paths.sessions_directory,
        data_paths=paths,
        command_permission=cast(str, values["command_permission"]),
        tool_scope=cast(str, values["tool_scope"]),
        model_context_window=cast(int | None, values["model_context_window"]),
        max_output_tokens=cast(int, values["max_output_tokens"]),
        context_safety_tokens=cast(int | None, values["context_safety_tokens"]),
        provider_input_limit=cast(int | None, values["provider_input_limit"]),
        compact_trigger_ratio=float(cast(float, values["compact_trigger_ratio"])),
        compact_target_ratio=float(cast(float, values["compact_target_ratio"])),
        global_instructions=Path(global_value).expanduser().resolve() if global_value else None,
        instructions_max_tokens=cast(int | None, values["instructions_max_tokens"]),
        max_model_calls=cast(int, values["max_model_calls"]),
        max_tool_calls=cast(int, values["max_tool_calls"]),
        max_failure_recoveries=cast(int, values["max_failure_recoveries"]),
        memory_auto_consolidation=auto,
        memory_headless_drain_seconds=float(drain),
        memory_input_token_limit=cast(int, input_limit),
        memory_output_token_limit=cast(int, output_limit),
        total_input_token_limit=cast(int, total_input),
        total_output_token_limit=cast(int, total_output),
        memory_embedding=embedding,
        cli_overrides=overrides,
    )
