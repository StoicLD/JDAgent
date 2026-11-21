"""为文件工具解析并校验工作区内的规范路径。"""

from pathlib import Path


class WorkspacePathError(ValueError):
    """表示无法确认请求路径位于工作区内。"""


class WorkspacePathResolver:
    """将工具提供的相对路径解析到工作区，读取已有文件和创建新文件分别检查真实祖先路径。"""

    def __init__(self, workspace: Path) -> None:
        try:
            self._root = workspace.resolve(strict=True)
        except OSError as error:
            raise WorkspacePathError("Workspace root does not exist") from error
        if not self._root.is_dir():
            raise WorkspacePathError("Workspace root is not a directory")

    @property
    def root(self) -> Path:
        return self._root

    def resolve_read(self, requested: str) -> Path:
        """校验读取目标的实际位置，拒绝工作区外部的目标。"""

        candidate = self._candidate(requested)
        try:
            resolved = candidate.resolve(strict=True)
        except OSError as error:
            raise WorkspacePathError("Read target does not exist") from error
        self._ensure_inside(resolved)
        if not resolved.is_file():
            raise WorkspacePathError("Read target is not a regular file")
        return resolved

    def resolve_write(self, requested: str) -> Path:
        """对尚未创建的路径先检查已存在的祖先，确保写入目标仍在工作区内。"""

        candidate = self._candidate(requested)
        try:
            resolved = candidate.resolve(strict=False)
        except OSError as error:
            raise WorkspacePathError("Write target cannot be resolved") from error
        self._ensure_inside(resolved)
        if resolved.exists() and not resolved.is_file():
            raise WorkspacePathError("Write target is not a regular file")
        return resolved

    def _candidate(self, requested: str) -> Path:
        if not requested or "\x00" in requested:
            raise WorkspacePathError("Path must be a non-empty filesystem path")
        path = Path(requested)
        return path if path.is_absolute() else self._root / path

    def _ensure_inside(self, candidate: Path) -> None:
        try:
            candidate.relative_to(self._root)
        except ValueError as error:
            raise WorkspacePathError("Path is outside the configured workspace") from error
