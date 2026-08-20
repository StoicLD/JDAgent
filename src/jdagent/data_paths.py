"""Resolve workspace identity and operating-system data locations."""

import hashlib
import json
import os
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path


def canonical_workspace_path(workspace: Path) -> Path:
    """Return the one canonical path used for workspace isolation."""

    resolved = workspace.resolve(strict=True)
    normalized = os.path.normpath(str(resolved))
    if os.name == "nt":
        normalized = os.path.normcase(normalized)
    return Path(normalized)


def workspace_identity(workspace: Path) -> str:
    """Hash a canonical workspace path without exposing it in directory names."""

    canonical = canonical_workspace_path(workspace)
    digest = hashlib.sha256(str(canonical).encode("utf-8")).hexdigest()
    return f"sha256-{digest}"


def _default_roots(
    *,
    platform_name: str,
    environment: Mapping[str, str],
    home: Path,
) -> tuple[Path, Path, str]:
    if platform_name == "win32":
        config_root = Path(environment.get("APPDATA", home / "AppData" / "Roaming"))
        data_root = Path(environment.get("LOCALAPPDATA", home / "AppData" / "Local"))
        return config_root, data_root, "JDAgent"
    if platform_name == "darwin":
        application_support = home / "Library" / "Application Support"
        return application_support, application_support, "JDAgent"
    config_root = Path(environment.get("XDG_CONFIG_HOME", home / ".config"))
    data_root = Path(environment.get("XDG_DATA_HOME", home / ".local" / "share"))
    return config_root, data_root, "jdagent"


@dataclass(frozen=True, slots=True)
class DataPaths:
    """All local paths derived once for a target workspace."""

    workspace: Path
    identity: str
    user_config_file: Path
    project_config_file: Path
    project_directory: Path
    sessions_directory: Path
    catalog_index: Path
    input_history: Path

    def ensure_project_partition(self) -> None:
        """Create or validate the collision-detecting workspace manifest."""

        self.project_directory.mkdir(parents=True, exist_ok=True)
        manifest_path = self.project_directory / "workspace.json"
        expected = {
            "canonical_path": str(self.workspace),
            "identity": self.identity,
        }
        if manifest_path.exists():
            try:
                actual = json.loads(manifest_path.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
                raise ValueError("Workspace partition manifest could not be validated") from error
            if actual != expected:
                raise ValueError("Workspace partition manifest does not match target workspace")
            return

        temporary_path = self.project_directory / f".workspace-{os.getpid()}.tmp"
        try:
            with temporary_path.open("x", encoding="utf-8", newline="\n") as stream:
                json.dump(expected, stream, ensure_ascii=False, sort_keys=True)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary_path, manifest_path)
        except OSError as error:
            raise ValueError("Workspace partition manifest could not be created") from error
        finally:
            try:
                temporary_path.unlink(missing_ok=True)
            except OSError:
                pass

    @classmethod
    def for_workspace(
        cls,
        workspace: Path,
        *,
        config_root: Path | None = None,
        data_root: Path | None = None,
        platform_name: str = sys.platform,
        environment: Mapping[str, str] = os.environ,
        home: Path | None = None,
    ) -> "DataPaths":
        """Resolve OS roots and the workspace partition without creating files."""

        canonical = canonical_workspace_path(workspace)
        default_config, default_data, application_name = _default_roots(
            platform_name=platform_name,
            environment=environment,
            home=home or Path.home(),
        )
        actual_config_root = config_root or default_config
        actual_data_root = data_root or default_data
        identity = workspace_identity(canonical)
        project_directory = actual_data_root / application_name / "projects" / identity
        return cls(
            workspace=canonical,
            identity=identity,
            user_config_file=actual_config_root / application_name / "config.toml",
            project_config_file=canonical / ".jdagent" / "config.toml",
            project_directory=project_directory,
            sessions_directory=project_directory / "sessions",
            catalog_index=project_directory / "catalog-index.json",
            input_history=project_directory / "input-history",
        )
