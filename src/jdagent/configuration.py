"""Layered CLI configuration resolved before runtime composition."""

import math
import os
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import cast

from jdagent.data_paths import DataPaths

_USER_FIELDS = frozenset(
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
    }
)
_PROJECT_FIELDS = frozenset(
    {
        "model",
        "model_timeout_seconds",
        "tool_timeout_seconds",
        "max_context_tokens",
        "fake_delay_seconds",
        "write_permission",
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
        "max_context_tokens": None,
        "fake_delay_seconds": 0.0,
        "write_permission": "ask",
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
    _apply_layer(values, user)
    _apply_layer(values, project)
    for item in fields(overrides):
        if item.name == "data_dir":
            continue
        value = getattr(overrides, item.name)
        if value is not None:
            values[item.name] = value
    _validate(values)

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
    )
