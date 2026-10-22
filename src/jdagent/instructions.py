"""Turn-scoped directory instructions with explicit source and path scope."""

import asyncio
import hashlib
from dataclasses import dataclass
from pathlib import Path

from jdagent.domain.json import JsonObject
from jdagent.domain.model import SystemPart


@dataclass(frozen=True, slots=True)
class DirectoryInstruction:
    path: str
    scope: str
    content_hash: str
    content: str


class TurnInstructions:
    """Discover only relevant ancestors, and never replace a version during its turn."""

    def __init__(self, workspace: Path, *, global_file: Path | None = None) -> None:
        self._workspace = workspace.resolve(strict=True)
        self._global_file = global_file
        self._seen: set[Path] = set()
        self._rules: list[DirectoryInstruction] = []
        self._started = False
        self._acknowledged = 0

    async def begin(self) -> None:
        if self._started:
            return
        await asyncio.to_thread(self._begin_sync)
        self._started = True

    def _begin_sync(self) -> None:
        if self._global_file is not None:
            self._load(self._global_file, "global default", allow_external=True)
        self._discover_sync(self._workspace)

    def _load(self, path: Path, scope: str, *, allow_external: bool = False) -> None:
        canonical = path.resolve(strict=False)
        if not allow_external and not canonical.is_relative_to(self._workspace):
            raise ValueError(f"Instruction file escapes workspace: {path}")
        if canonical in self._seen:
            return
        try:
            raw = canonical.read_bytes()
        except FileNotFoundError:
            self._seen.add(canonical)
            return
        except OSError as error:
            raise ValueError(f"Cannot read instruction file: {path}") from error
        try:
            content = raw.decode("utf-8-sig")
        except UnicodeDecodeError as error:
            raise ValueError(f"Instruction file is not UTF-8: {path}") from error
        self._seen.add(canonical)
        self._rules.append(
            DirectoryInstruction(str(canonical), scope, hashlib.sha256(raw).hexdigest(), content)
        )

    def _discover_sync(self, target: Path) -> None:
        path = target.resolve(strict=False)
        if not path.is_relative_to(self._workspace):
            raise ValueError("Instruction target is outside workspace")
        directory = path if path.is_dir() else path.parent
        ancestors = [directory, *directory.parents]
        for ancestor in reversed(ancestors):
            if ancestor.is_relative_to(self._workspace):
                self._load(ancestor / "AGENTS.md", str(ancestor))

    async def discover_arguments(self, arguments: JsonObject) -> None:
        await self.begin()
        targets: list[Path] = []
        for key in ("path", "directory", "cwd"):
            value = arguments.get(key)
            if isinstance(value, str):
                targets.append(self._workspace / value)
        args = arguments.get("args")
        if isinstance(args, list):
            for value in args:
                if isinstance(value, str) and not value.startswith("-"):
                    candidate = (self._workspace / value).resolve(strict=False)
                    if candidate.is_relative_to(self._workspace) and candidate.exists():
                        targets.append(candidate)
        for target in targets:
            await asyncio.to_thread(self._discover_sync, target)

    @property
    def needs_replan(self) -> bool:
        return len(self._rules) > self._acknowledged

    def acknowledge(self) -> None:
        """Only request preparation may acknowledge rules actually sent to the model."""
        self._acknowledged = len(self._rules)

    def system_parts(self) -> tuple[SystemPart, ...]:
        return tuple(
            SystemPart(
                f"Directory instructions. Scope: {rule.scope}\nSource: {rule.path}\n"
                f"Version: {rule.content_hash}\n{rule.content}",
                "directory_instructions",
            )
            for rule in self._rules
        )
