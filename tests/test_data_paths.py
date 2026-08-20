import json
from pathlib import Path

import pytest

from jdagent.data_paths import DataPaths, workspace_identity


def test_workspace_identity_is_stable_for_equivalent_paths(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    direct = workspace_identity(workspace)
    equivalent = workspace_identity(workspace / ".")

    assert direct == equivalent
    assert direct.startswith("sha256-")
    assert len(direct) == len("sha256-") + 64


def test_data_paths_partition_sessions_outside_workspace(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    config_root = tmp_path / "config"
    data_root = tmp_path / "data"

    paths = DataPaths.for_workspace(
        workspace,
        config_root=config_root,
        data_root=data_root,
        platform_name="linux",
    )

    assert paths.user_config_file == config_root / "jdagent" / "config.toml"
    assert paths.project_directory.parent == data_root / "jdagent" / "projects"
    assert paths.sessions_directory == paths.project_directory / "sessions"
    assert paths.catalog_index == paths.project_directory / "catalog-index.json"
    assert paths.input_history == paths.project_directory / "input-history"
    assert workspace not in paths.project_directory.parents


def test_partition_manifest_fails_closed_on_identity_mismatch(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    paths = DataPaths.for_workspace(
        workspace,
        config_root=tmp_path / "config",
        data_root=tmp_path / "data",
        platform_name="linux",
    )

    paths.ensure_project_partition()
    manifest_path = paths.project_directory / "workspace.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest == {
        "canonical_path": str(paths.workspace),
        "identity": paths.identity,
    }

    manifest_path.write_text(
        json.dumps({"canonical_path": str(tmp_path / "other"), "identity": paths.identity}),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="Workspace partition manifest does not match"):
        paths.ensure_project_partition()
