"""Canonical workspace path resolution shared by file tools."""

from pathlib import Path


class WorkspacePathError(ValueError):
    """A requested path cannot be proven to remain inside the workspace."""


class WorkspacePathResolver:
    """Resolve existing reads and potential writes without prefix comparisons."""

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
        """Resolve an existing file while rejecting every workspace escape."""

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
        """Resolve a possibly missing file through its real existing ancestors."""

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
