"""读取 MCP 服务器声明，按项目配置收窄可用范围。"""

import math
import re
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Literal, cast
from urllib.parse import urlsplit


class MCPConfigurationError(ValueError):
    """表示服务配置校验失败，报错只描述字段或约束，避免回显凭据等输入值。"""


@dataclass(frozen=True, slots=True)
class MCPToolOverride:
    """保存针对一个已授权工具的超时设置，以及服务端明确超时后是否允许重试。"""

    name: str
    timeout_seconds: float
    safe_repeat: bool = False


@dataclass(frozen=True, slots=True)
class MCPServerConfiguration:
    """描述一个服务的连接方式、可用能力与时限；敏感配置只在建立连接时解析。"""

    server_id: str
    transport: Literal["stdio", "http"]
    enabled: bool
    tools: tuple[str, ...]
    resources: bool
    prompts: bool
    command: str | None = field(default=None, repr=False)
    args: tuple[str, ...] = field(default=(), repr=False)
    cwd: Path | None = None
    url: str | None = field(default=None, repr=False)
    bearer_env: str | None = None
    environment_refs: tuple[tuple[str, str], ...] = ()
    timeout_seconds: float = 600.0
    tool_overrides: tuple[MCPToolOverride, ...] = ()


@dataclass(frozen=True, slots=True)
class MCPConfiguration:
    """汇总当前可配置的 MCP 服务和会话结果的存储配额。"""

    servers: tuple[MCPServerConfiguration, ...]
    result_quota_mib: int = 100


def _table(value: object, label: str) -> dict[str, object]:
    if not isinstance(value, dict) or any(
        not isinstance(key, str) for key in cast(dict[object, object], value)
    ):
        raise MCPConfigurationError(f"{label} must be a table")
    return cast(dict[str, object], value)


def _keys(value: dict[str, object], allowed: set[str], label: str) -> None:
    if set(value) - allowed:
        raise MCPConfigurationError(f"{label} contains forbidden or unknown fields")


def _string(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip() or "\x00" in value:
        raise MCPConfigurationError(f"{label} must be a nonempty string without NUL")
    return value


def _boolean(value: object, label: str) -> bool:
    if not isinstance(value, bool):
        raise MCPConfigurationError(f"{label} must be boolean")
    return value


def _names(value: object, label: str) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise MCPConfigurationError(f"{label} must be an array")
    names = tuple(_string(item, label) for item in cast(list[object], value))
    if len(set(names)) != len(names) or "*" in names:
        raise MCPConfigurationError(f"{label} requires unique explicit names")
    return names


def _env_reference(value: object, label: str) -> str:
    name = _string(value, label)
    if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name) is None:
        raise MCPConfigurationError(f"{label} must name an environment variable")
    return name


def _duration(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise MCPConfigurationError("MCP timeout_seconds must be a positive finite number")
    try:
        duration = float(value)
    except OverflowError:
        raise MCPConfigurationError("MCP timeout_seconds is too large") from None
    if not math.isfinite(duration) or duration <= 0:
        raise MCPConfigurationError("MCP timeout_seconds must be a positive finite number")
    return duration


def _server(
    server_id: str, value: object, workspace: Path, user_directory: Path, default_timeout: float
) -> MCPServerConfiguration:
    """校验单个服务的标识、传输和授权设置，区分标准输入输出与 HTTP 连接各自允许的字段。"""
    if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", server_id) is None:
        raise MCPConfigurationError(
            "MCP Server ID must use 1-64 letters, digits, underscores or hyphens"
        )
    row = _table(value, "MCP Server")
    _keys(
        row,
        {
            "transport",
            "enabled",
            "tools",
            "resources",
            "prompts",
            "command",
            "args",
            "cwd",
            "url",
            "bearer_env",
            "env",
            "timeout_seconds",
            "tool_overrides",
        },
        "MCP Server",
    )
    transport = row.get("transport")
    if transport not in ("stdio", "http"):
        raise MCPConfigurationError("MCP transport must be stdio or http")
    shared = MCPServerConfiguration(
        server_id=server_id,
        transport=transport,
        enabled=_boolean(row.get("enabled", False), "MCP enabled"),
        tools=_names(row.get("tools", []), "MCP tools"),
        resources=_boolean(row.get("resources", False), "MCP resources"),
        prompts=_boolean(row.get("prompts", False), "MCP prompts"),
        timeout_seconds=_duration(row.get("timeout_seconds", default_timeout)),
    )
    overrides = _table(row.get("tool_overrides", {}), "MCP tool_overrides")
    policies: list[MCPToolOverride] = []
    for name, raw_policy in overrides.items():
        if name not in shared.tools:
            raise MCPConfigurationError("MCP tool_overrides may only name allowed tools")
        policy = _table(raw_policy, "MCP tool override")
        _keys(policy, {"timeout_seconds", "safe_repeat"}, "MCP tool override")
        policies.append(
            MCPToolOverride(
                name,
                _duration(policy.get("timeout_seconds", shared.timeout_seconds)),
                _boolean(policy.get("safe_repeat", False), "MCP safe_repeat"),
            )
        )
    shared = replace(shared, tool_overrides=tuple(policies))
    if transport == "stdio":
        if "url" in row or "bearer_env" in row:
            raise MCPConfigurationError("stdio does not accept HTTP configuration")
        command = _string(row.get("command"), "MCP command")
        raw_args = row.get("args", [])
        if not isinstance(raw_args, list) or any(
            not isinstance(item, str) or "\x00" in item for item in cast(list[object], raw_args)
        ):
            raise MCPConfigurationError("MCP args must be strings without NUL")
        cwd = workspace
        if "cwd" in row:
            supplied = Path(_string(row["cwd"], "MCP cwd")).expanduser()
            cwd = (supplied if supplied.is_absolute() else user_directory / supplied).resolve()
        environment = _table(row.get("env", {}), "MCP env")
        refs = tuple(
            sorted(
                (_env_reference(key, "MCP env key"), _env_reference(ref, "MCP env reference"))
                for key, ref in environment.items()
            )
        )
        return replace(
            shared,
            command=command,
            args=tuple(cast(list[str], raw_args)),
            cwd=cwd,
            environment_refs=refs,
        )
    if any(key in row for key in ("command", "args", "cwd", "env")):
        raise MCPConfigurationError("HTTP does not accept stdio configuration")
    url = _string(row.get("url"), "MCP URL")
    try:
        parsed = urlsplit(url)
        valid = parsed.scheme in {"http", "https"} and bool(parsed.hostname)
        valid = (
            valid and parsed.username is None and parsed.password is None and not parsed.fragment
        )
        _ = parsed.port
    except ValueError:
        valid = False
    if not valid:
        raise MCPConfigurationError(
            "MCP URL requires HTTP(S), a host, and no embedded credentials or fragment"
        )
    bearer = _env_reference(row["bearer_env"], "MCP bearer_env") if "bearer_env" in row else None
    return replace(shared, url=url, bearer_env=bearer)


def resolve_mcp_configuration(
    user: object, project: object, *, workspace: Path, user_directory: Path
) -> MCPConfiguration:
    """以用户声明为上限合并项目的范围限制，校验凭据引用。此阶段只解析配置，无需连接或启动服务。"""
    user_table = _table(user, "User MCP")
    project_table = _table(project, "Project MCP")
    _keys(user_table, {"servers", "timeout_seconds", "result_quota_mib"}, "User MCP")
    _keys(project_table, {"servers"}, "Project MCP")
    definitions = _table(user_table.get("servers", {}), "User MCP servers")
    restrictions = _table(project_table.get("servers", {}), "Project MCP servers")
    if set(restrictions) - set(definitions):
        raise MCPConfigurationError("Project MCP references an undefined Server")
    resolved: list[MCPServerConfiguration] = []
    quota = user_table.get("result_quota_mib", 100)
    if isinstance(quota, bool) or not isinstance(quota, int) or quota <= 0:
        raise MCPConfigurationError("MCP result_quota_mib must be a positive integer")
    default_timeout = _duration(user_table.get("timeout_seconds", 600.0))
    for name, definition in definitions.items():
        server = _server(name, definition, workspace, user_directory, default_timeout)
        narrowed = _table(restrictions.get(name, {}), "Project MCP Server")
        _keys(narrowed, {"enabled", "tools", "resources", "prompts"}, "Project MCP Server")
        enabled = _boolean(narrowed.get("enabled", server.enabled), "Project MCP enabled")
        resources = _boolean(narrowed.get("resources", server.resources), "Project MCP resources")
        prompts = _boolean(narrowed.get("prompts", server.prompts), "Project MCP prompts")
        tools = (
            _names(narrowed["tools"], "Project MCP tools") if "tools" in narrowed else server.tools
        )
        if (
            (enabled and not server.enabled)
            or (resources and not server.resources)
            or (prompts and not server.prompts)
            or not set(tools).issubset(server.tools)
        ):
            raise MCPConfigurationError("Project MCP cannot expand user permissions")
        resolved.append(
            replace(
                server,
                enabled=enabled,
                resources=resources,
                prompts=prompts,
                tools=tools,
                tool_overrides=tuple(
                    policy for policy in server.tool_overrides if policy.name in tools
                ),
            )
        )
    return MCPConfiguration(tuple(resolved), quota)
