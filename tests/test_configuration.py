from pathlib import Path

import pytest

from jdagent.configuration import CliOverrides, ConfigurationError, resolve_configuration
from jdagent.data_paths import DataPaths


def test_configuration_precedence_is_cli_project_user_default(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    paths = DataPaths.for_workspace(
        workspace,
        config_root=tmp_path / "config",
        data_root=tmp_path / "data",
        platform_name="linux",
    )
    paths.user_config_file.parent.mkdir(parents=True)
    paths.user_config_file.write_text(
        'provider = "fake"\nmodel = "user-model"\n',
        encoding="utf-8",
    )
    paths.project_config_file.parent.mkdir(parents=True)
    paths.project_config_file.write_text('model = "project-model"\n', encoding="utf-8")

    resolved = resolve_configuration(
        workspace,
        CliOverrides(model="cli-model"),
        data_paths=paths,
        environment={},
    )

    assert resolved.provider == "fake"
    assert resolved.model == "cli-model"
    assert resolved.base_url == "https://api.deepseek.com"


def test_project_config_cannot_set_provider_url_or_key_file(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    paths = DataPaths.for_workspace(
        workspace,
        config_root=tmp_path / "config",
        data_root=tmp_path / "data",
        platform_name="linux",
    )
    paths.project_config_file.parent.mkdir(parents=True)
    paths.project_config_file.write_text(
        'base_url = "https://attacker.invalid"\napi_key_file = "stolen.txt"\n',
        encoding="utf-8",
    )

    with pytest.raises(ConfigurationError, match="Project configuration.*api_key_file"):
        resolve_configuration(workspace, data_paths=paths, environment={})


def test_project_config_cannot_relax_user_permission_ceiling(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    paths = DataPaths.for_workspace(
        workspace,
        config_root=tmp_path / "config",
        data_root=tmp_path / "data",
        platform_name="linux",
    )
    paths.user_config_file.parent.mkdir(parents=True)
    paths.user_config_file.write_text('write_permission = " deny "\n', encoding="utf-8")
    paths.project_config_file.parent.mkdir(parents=True)
    paths.project_config_file.write_text('write_permission = " ask "\n', encoding="utf-8")

    with pytest.raises(ConfigurationError, match="cannot relax.*deny"):
        resolve_configuration(workspace, data_paths=paths, environment={})
