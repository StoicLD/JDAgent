import json
from pathlib import Path

import pytest

from jdagent.cli import main
from jdagent.configuration import CliOverrides, resolve_configuration
from jdagent.data_paths import DataPaths


def config_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> DataPaths:
    for name in ("APPDATA", "XDG_CONFIG_HOME"):
        monkeypatch.setenv(name, str(tmp_path / "config"))
    for name in ("LOCALAPPDATA", "XDG_DATA_HOME"):
        monkeypatch.setenv(name, str(tmp_path / "data"))
    paths = DataPaths.for_workspace(tmp_path)
    paths.user_config_file.parent.mkdir(parents=True)
    paths.project_config_file.parent.mkdir(parents=True)
    return paths


def test_cli_accepts_narrowed_mcp_configuration_without_connecting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    paths = config_paths(tmp_path, monkeypatch)
    paths.user_config_file.write_text(
        '[mcp.servers.docs]\nenabled = true\ntransport = "stdio"\n'
        'command = "not-installed-until-mcp-use"\ntools = ["read", "search"]\n'
        "resources = true\nprompts = true\n"
        '[mcp.servers.docs.env]\nAPI_TOKEN = "UNSET_MCP_TOKEN"\n',
        encoding="utf-8",
    )
    paths.project_config_file.write_text(
        '[mcp.servers.docs]\ntools = ["read"]\nprompts = false\n', encoding="utf-8"
    )
    monkeypatch.delenv("UNSET_MCP_TOKEN", raising=False)
    code = main(["hello", "--provider", "fake", "--workspace", str(tmp_path), "--output", "json"])
    captured = capsys.readouterr()
    assert code == 0, captured.err
    assert json.loads(captured.out)["status"] == "success"
    assert captured.err == ""
    configured = resolve_configuration(tmp_path, CliOverrides(provider="fake"), data_paths=paths)
    assert configured.mcp_servers[0].tools == ("read",)
    assert configured.mcp_servers[0].prompts is False
    assert configured.mcp_servers[0].environment_refs == (("API_TOKEN", "UNSET_MCP_TOKEN"),)


@pytest.mark.parametrize(
    "override",
    [
        'command = "arbitrary-executable"',
        'args = ["extra"]',
        'cwd = "other"',
        'url = "https://other.invalid/mcp"',
        'bearer_env = "OTHER_ACCOUNT"',
        'env = { TOKEN = "OTHER_ACCOUNT" }',
        'transport = "stdio"',
        'tools = ["write"]',
        "resources = true",
        "prompts = true",
    ],
)
def test_cli_rejects_project_expansion_before_creating_a_session(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    override: str,
) -> None:
    paths = config_paths(tmp_path, monkeypatch)
    paths.user_config_file.write_text(
        '[mcp.servers.docs]\nenabled = true\ntransport = "http"\n'
        'url = "https://approved.invalid/mcp"\nbearer_env = "MCP_DOCS_TOKEN"\n'
        'tools = ["read"]\n',
        encoding="utf-8",
    )
    paths.project_config_file.write_text(f"[mcp.servers.docs]\n{override}\n", encoding="utf-8")
    code = main(["hello", "--provider", "fake", "--workspace", str(tmp_path), "--output", "json"])
    captured = capsys.readouterr()
    assert code == 2
    assert captured.out == ""
    assert "Project MCP" in captured.err
    assert not paths.sessions_directory.exists()


@pytest.mark.parametrize(
    "user_body",
    [
        'url = "https://user:NEVER_PRINT_SECRET@approved.invalid/mcp"',
        'url = "https://approved.invalid/mcp"\nbearer_token = "NEVER_PRINT_SECRET"',
        'url = "https://approved.invalid/mcp"\nbearer_env = "NEVER_PRINT_SECRET VALUE"',
    ],
)
def test_cli_rejects_inline_mcp_credentials_without_echoing_them(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    user_body: str,
) -> None:
    paths = config_paths(tmp_path, monkeypatch)
    paths.user_config_file.write_text(
        f'[mcp.servers.docs]\ntransport = "http"\n{user_body}\n', encoding="utf-8"
    )
    code = main(["hello", "--provider", "fake", "--workspace", str(tmp_path)])
    captured = capsys.readouterr()
    assert code == 2
    assert "NEVER_PRINT_SECRET" not in captured.out + captured.err


def test_project_cannot_enable_disabled_server_or_define_a_new_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    paths = config_paths(tmp_path, monkeypatch)
    paths.user_config_file.write_text(
        '[mcp.servers.docs]\ntransport = "http"\nurl = "https://approved.invalid/mcp"\n',
        encoding="utf-8",
    )
    for project_body in ("[mcp.servers.docs]\nenabled = true\n", "[mcp.servers.unknown]\n"):
        paths.project_config_file.write_text(project_body, encoding="utf-8")
        code = main(["hello", "--provider", "fake", "--workspace", str(tmp_path)])
        assert code == 2
        assert "Project MCP" in capsys.readouterr().err


def test_cli_accepts_explicit_long_call_budget_and_safe_repeat_policy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    paths = config_paths(tmp_path, monkeypatch)
    paths.user_config_file.write_text(
        "[mcp]\ntimeout_seconds = 700\n"
        '[mcp.servers.docs]\nenabled = true\ntransport = "http"\n'
        'url = "https://approved.invalid/mcp"\ntools = ["read", "search"]\ntimeout_seconds = 1200\n'
        "[mcp.servers.docs.tool_overrides.read]\ntimeout_seconds = 3600\nsafe_repeat = true\n",
        encoding="utf-8",
    )
    code = main(["hello", "--provider", "fake", "--workspace", str(tmp_path)])
    assert code == 0, capsys.readouterr().err
    configured = resolve_configuration(tmp_path, CliOverrides(provider="fake"), data_paths=paths)
    server = configured.mcp_servers[0]
    assert server.timeout_seconds == 1200
    assert server.tool_overrides[0].name == "read"
    assert server.tool_overrides[0].timeout_seconds == 3600
    assert server.tool_overrides[0].safe_repeat is True


@pytest.mark.parametrize("quota", ["0", "-1", "true", "1.5", '"100"', "inf"])
def test_cli_rejects_invalid_mcp_result_quota_before_creating_a_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], quota: str
) -> None:
    paths = config_paths(tmp_path, monkeypatch)
    paths.user_config_file.write_text(f"[mcp]\nresult_quota_mib = {quota}\n", encoding="utf-8")
    code = main(["hello", "--provider", "fake", "--workspace", str(tmp_path)])
    assert code == 2
    assert "result_quota_mib must be a positive integer" in capsys.readouterr().err
    assert not paths.sessions_directory.exists()


def test_project_cannot_change_mcp_result_quota(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    paths = config_paths(tmp_path, monkeypatch)
    paths.project_config_file.write_text("[mcp]\nresult_quota_mib = 200\n", encoding="utf-8")
    code = main(["hello", "--provider", "fake", "--workspace", str(tmp_path)])
    assert code == 2
    assert "Project MCP" in capsys.readouterr().err
    assert not paths.sessions_directory.exists()


@pytest.mark.parametrize("duration", ["0", "-1", "nan", "inf", "true", '"600"'])
def test_cli_rejects_invalid_mcp_wait_budget(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    duration: str,
) -> None:
    paths = config_paths(tmp_path, monkeypatch)
    paths.user_config_file.write_text(f"[mcp]\ntimeout_seconds = {duration}\n", encoding="utf-8")
    code = main(["hello", "--provider", "fake", "--workspace", str(tmp_path)])
    assert code == 2
    assert "positive finite number" in capsys.readouterr().err


def test_project_narrowing_drops_execution_overrides_for_removed_tools(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = config_paths(tmp_path, monkeypatch)
    paths.user_config_file.write_text(
        "[mcp]\ntimeout_seconds = 900\n"
        '[mcp.servers.docs]\ntransport = "stdio"\ncommand = "server"\n'
        'tools = ["read", "search"]\ncwd = "servers"\n'
        "[mcp.servers.docs.tool_overrides.read]\nsafe_repeat = true\n",
        encoding="utf-8",
    )
    paths.project_config_file.write_text(
        '[mcp.servers.docs]\ntools = ["search"]\n', encoding="utf-8"
    )
    configured = resolve_configuration(tmp_path, CliOverrides(provider="fake"), data_paths=paths)
    server = configured.mcp_servers[0]
    assert server.tools == ("search",)
    assert server.tool_overrides == ()
    assert server.timeout_seconds == 900
    assert server.cwd == (paths.user_config_file.parent / "servers").resolve()
    assert server.enabled is False
