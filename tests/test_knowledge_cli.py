import json
from pathlib import Path

import pytest

from jdagent.cli import main
from jdagent.knowledge.errors import KnowledgeErrorCode
from jdagent.tools.builtins import create_builtin_tools
from jdagent.tools.workspace import WorkspacePathResolver


def _isolate_data(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APPDATA", str(tmp_path / "config"))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "data"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)


def test_knowledge_cli_manages_connections_kb_and_bindings(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _isolate_data(tmp_path, monkeypatch)

    add_code = main(
        [
            "--workspace",
            str(workspace),
            "knowledge",
            "connection",
            "add",
            "--name",
            "local",
        ]
    )
    connection = json.loads(capsys.readouterr().out)
    kb_code = main(
        [
            "--workspace",
            str(workspace),
            "knowledge",
            "kb",
            "create",
            "--connection",
            connection["connection_id"],
            "--name",
            "hr",
        ]
    )
    kb = json.loads(capsys.readouterr().out)
    bind_code = main(
        [
            "--workspace",
            str(workspace),
            "knowledge",
            "binding",
            "add",
            "--connection",
            connection["connection_id"],
            "--kb",
            kb["knowledge_base_id"],
        ]
    )
    binding = json.loads(capsys.readouterr().out)
    list_code = main(["--workspace", str(workspace), "knowledge", "binding", "list"])
    listed = json.loads(capsys.readouterr().out)

    assert add_code == kb_code == bind_code == list_code == 0
    assert listed[0]["binding_id"] == binding["binding_id"]
    assert connection["provider_kind"] == "jdagent_managed"

    deny_code = main(
        [
            "--workspace",
            str(workspace),
            "knowledge",
            "kb",
            "delete",
            "--id",
            kb["knowledge_base_id"],
        ]
    )
    captured = capsys.readouterr()
    assert deny_code == 2
    assert KnowledgeErrorCode.CONFIRMATION_REQUIRED.value in captured.err


def test_ordinary_agent_turn_does_not_create_knowledge_catalog(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _isolate_data(tmp_path, monkeypatch)
    code = main(["hello", "--provider", "fake", "--workspace", str(workspace)])
    assert code == 0
    assert capsys.readouterr().out == "Offline fake model response.\n"
    data_root = tmp_path / "data"
    assert not list(data_root.rglob("catalog.sqlite"))


def test_corrupt_catalog_does_not_block_ordinary_agent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _isolate_data(tmp_path, monkeypatch)
    from jdagent.data_paths import DataPaths

    paths = DataPaths.for_workspace(
        workspace,
        config_root=tmp_path / "config",
        data_root=tmp_path / "data",
    )
    paths.knowledge_catalog.parent.mkdir(parents=True)
    paths.knowledge_catalog.write_bytes(b"corrupt-catalog")
    code = main(["hello", "--provider", "fake", "--workspace", str(workspace)])
    assert code == 0


def test_knowledge_commands_are_not_registered_as_tools(tmp_path: Path) -> None:
    names = {tool.name for tool in create_builtin_tools(WorkspacePathResolver(tmp_path))}
    assert "knowledge" not in names
    assert "connection" not in names
    assert names == {"calculator", "read_text_file", "write_text_file"}
