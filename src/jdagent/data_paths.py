"""确定工作区身份，并定位当前操作系统上的项目数据目录。"""

import hashlib
import json
import os
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path


def canonical_workspace_path(workspace: Path) -> Path:
    """解析工作区的实际路径，并按平台统一大小写等路径表示，用于识别同一工作区。"""

    resolved = workspace.resolve(strict=True)
    normalized = os.path.normpath(str(resolved))
    if os.name == "nt":
        normalized = os.path.normcase(normalized)
    return Path(normalized)


def workspace_identity(workspace: Path) -> str:
    """对规范化后的工作区路径求摘要，得到可用作目录名的稳定标识。"""

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
    """保存一个工作区对应的配置、会话和知识数据位置。"""

    workspace: Path
    identity: str
    user_config_file: Path
    project_config_file: Path
    project_directory: Path
    sessions_directory: Path
    catalog_index: Path
    input_history: Path
    knowledge_directory: Path
    knowledge_catalog: Path
    knowledge_objects: Path
    knowledge_backups: Path

    def ensure_project_partition(self) -> None:
        """校验数据分区中登记的工作区身份；首次使用时创建身份清单，发现冲突则停止。"""

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
        """根据平台和工作区推导各类文件的位置。实际创建目录由使用这些路径的组件负责。"""

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
        knowledge_directory = actual_data_root / application_name / "knowledge"
        return cls(
            workspace=canonical,
            identity=identity,
            user_config_file=actual_config_root / application_name / "config.toml",
            project_config_file=canonical / ".jdagent" / "config.toml",
            project_directory=project_directory,
            sessions_directory=project_directory / "sessions-v5",
            catalog_index=project_directory / "catalog-index.json",
            input_history=project_directory / "input-history",
            knowledge_directory=knowledge_directory,
            knowledge_catalog=knowledge_directory / "catalog.sqlite",
            knowledge_objects=knowledge_directory / "objects",
            knowledge_backups=knowledge_directory / "backups",
        )
