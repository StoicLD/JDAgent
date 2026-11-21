"""提供有读取上限的 UTF-8 文件工具，依据整份文件的版本执行精确编辑。"""

import asyncio
import hashlib
import json
import os
import re
import time
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path

from pathspec import GitIgnoreSpec

from jdagent.adapters.windows_files import locked_ancestors, locked_file
from jdagent.domain.json import (
    JsonObject,
    JsonValue,
    require_array,
    require_integer,
    require_object,
    require_string,
)
from jdagent.domain.tools import (
    RiskLevel,
    ToolDefinition,
    ToolErrorCode,
    ToolExecutionContext,
    ToolOutcome,
)
from jdagent.tools.builtins import object_schema
from jdagent.tools.workspace import WorkspacePathError

MAX_FILE = 2 * 1024 * 1024
MAX_REQUEST = 512 * 1024
MAX_OUTPUT = 32 * 1024
_SKIP = {".git", "node_modules", ".venv", "venv", "__pycache__", "dist", "build"}


class FileToolError(ValueError):
    """表示文件修改开始前已能确定操作被拒绝。"""

    def __init__(self, code: ToolErrorCode, message: str) -> None:
        super().__init__(message)
        self.code = code


def checked_path(root: Path, requested: str, *, directory: bool = False) -> Path:
    """校验目标及其路径组成，拒绝越出工作区、指向设备或使用不受支持别名的路径。"""
    supplied = Path(requested)
    if any(
        part != supplied.anchor
        and part not in {".", ".."}
        and (":" in part or part.endswith((" ", ".")) or Path(part).is_reserved())
        for part in supplied.parts
    ):
        raise WorkspacePathError("Device, stream or ambiguous path")
    if not requested or "\x00" in requested or requested.startswith(("\\\\", "//")):
        raise WorkspacePathError("Unsupported path")
    candidate = Path(os.path.abspath(supplied if supplied.is_absolute() else root / supplied))
    try:
        relative = candidate.relative_to(root)
    except ValueError as error:
        raise WorkspacePathError("Path outside workspace") from error
    walk = root
    for part in relative.parts:
        if ":" in part or part.endswith((" ", ".")) or Path(part).is_reserved():
            raise WorkspacePathError("Device, stream or ambiguous path")
        walk = walk / part
        if walk.exists() or walk.is_symlink():
            status = walk.lstat()
            if walk.is_symlink() or getattr(status, "st_file_attributes", 0) & 0x400:
                raise WorkspacePathError("Reparse points and links are unsupported")
            if walk.is_file() and status.st_nlink != 1:
                raise WorkspacePathError("Hard links are unsupported")
    if directory and not candidate.is_dir():
        raise WorkspacePathError("Directory does not exist")
    if not directory and candidate.exists() and not candidate.is_file():
        raise WorkspacePathError("Not a regular file")
    return candidate


def revision(path: Path, raw: bytes) -> str:
    """根据规范化的文件路径和完整字节生成版本标识，供编辑前检查文件身份和内容。"""
    return (
        "sha256:" + hashlib.sha256(os.path.normcase(str(path)).encode() + b"\x00" + raw).hexdigest()
    )


def decode_text(raw: bytes) -> str:
    if len(raw) > MAX_FILE:
        raise FileToolError(ToolErrorCode.CAPACITY, "File exceeds 2 MiB")
    if b"\x00" in raw:
        raise FileToolError(ToolErrorCode.UNSUPPORTED, "Binary text is unsupported")
    try:
        return raw.decode("utf-8-sig")
    except UnicodeDecodeError as error:
        raise FileToolError(ToolErrorCode.UNSUPPORTED, "Only UTF-8 text is supported") from error


def edit_bytes(raw: bytes, edits: list[JsonValue]) -> bytes:
    """以同一份原始内容定位每项编辑并检查匹配范围，保留未被修改的字节。"""
    text = decode_text(raw)
    normalized = text.replace("\r\n", "\n")
    offsets = [0]
    position = 0
    while position < len(text):
        position += 2 if text[position : position + 2] == "\r\n" else 1
        offsets.append(position)
    replacements: list[tuple[int, int, str]] = []
    for item in edits:
        edit = require_object(item, "edit")
        old = require_string(edit, "old_text").replace("\r\n", "\n")
        new = require_string(edit, "new_text").replace("\r\n", "\n")
        start = normalized.find(old)
        if not old or start < 0 or normalized.find(old, start + 1) >= 0:
            raise FileToolError(ToolErrorCode.CONFLICT, "Old text must match exactly once")
        begin, end = offsets[start], offsets[start + len(old)]
        styles = set(re.findall(r"\r\n|(?<!\r)\n", text[begin:end]))
        if len(styles) > 1:
            raise FileToolError(ToolErrorCode.CONFLICT, "Matched region has mixed newlines")
        first_newline = re.search(r"\r\n|(?<!\r)\n", text)
        newline = next(iter(styles), first_newline.group() if first_newline else "\n")
        replacements.append((begin, end, new.replace("\n", newline)))
    replacements.sort()
    if any(left[1] > right[0] for left, right in zip(replacements, replacements[1:], strict=False)):
        raise FileToolError(ToolErrorCode.CONFLICT, "Edit regions overlap")
    for begin, end, replacement in reversed(replacements):
        text = text[:begin] + replacement + text[end:]
    result = (b"\xef\xbb\xbf" if raw.startswith(b"\xef\xbb\xbf") else b"") + text.encode("utf-8")
    decode_text(result)
    return result


def _read(path: Path) -> tuple[bytes, str]:
    with locked_file(path) as stream:
        raw = stream.read(MAX_FILE + 1)
    return raw, decode_text(raw)


def _read_result(path: Path, args: JsonObject) -> ToolOutcome:
    raw, text = _read(path)
    lines = text.splitlines(keepends=True)
    start = require_integer(args, "start_line", 1)
    count = require_integer(args, "line_count", 200)
    selected: list[str] = []
    size = 0
    for line in lines[start - 1 : start - 1 + count]:
        if size + len(line.encode("utf-8")) > MAX_OUTPUT:
            if not selected:
                raise FileToolError(ToolErrorCode.CAPACITY, "Line exceeds output limit")
            break
        selected.append(line)
        size += len(line.encode("utf-8"))
    following = start + len(selected)
    return ToolOutcome(
        data={
            "content": "".join(selected),
            "start_line": start,
            "next_line": following if following <= len(lines) else None,
            "revision": revision(path, raw),
            "encoding": "utf-8-sig" if raw.startswith(b"\xef\xbb\xbf") else "utf-8",
        }
    )


def _mutate(path: Path, args: JsonObject, *, create: bool) -> ToolOutcome:
    """在受保护的文件访问期间核对目标和预期版本，再执行写入或编辑。"""
    if not path.parent.is_dir():
        raise FileToolError(ToolErrorCode.INVALID_ARGUMENTS, "Parent directory must exist")
    content = require_string(args, "content").encode("utf-8") if create else b""
    if create:
        decode_text(content)
    with locked_file(path, create=create, write=True) as stream:
        before = b"" if create else stream.read(MAX_FILE + 1)
        before_revision = revision(path, before)
        if not create:
            if before_revision != require_string(args, "revision"):
                raise FileToolError(ToolErrorCode.CONFLICT, "File revision changed; read again")
            content = edit_bytes(before, require_array(args.get("edits"), "edits"))
        try:
            stream.seek(0)
            stream.write(content)
            stream.truncate()
            stream.flush()
            os.fsync(stream.fileno())
        except OSError:
            return ToolOutcome(
                completed=False,
                effect="unknown",
                error_code=ToolErrorCode.UNCERTAIN,
                message="File commit could not be confirmed",
            )
    return ToolOutcome(
        data={
            "revision": revision(path, content),
            "previous_revision": before_revision,
            "bytes_written": len(content),
        },
        effect="committed",
    )


def _scan(root: Path, args: JsonObject, *, search: bool) -> ToolOutcome:
    """在规定范围和扫描预算内枚举文件或查找匹配文本，记录未能完整搜索的情况。"""
    directory = checked_path(root, str(args.get("directory", ".")), directory=True)
    include = args.get("include_ignored", False) is True
    limit = require_integer(args, "limit", 100)
    pattern = str(args.get("pattern", "**/*"))
    query = str(args.get("query", ""))
    context = require_integer(args, "context_lines", 0)
    output: list[JsonValue] = []
    skipped = 0
    scanned = 0
    output_bytes = 0
    deadline = time.monotonic() + 10
    truncated = False
    inherited_rules: list[tuple[Path, GitIgnoreSpec]] = []
    if not include:
        ancestors = [
            root,
            *(
                root / Path(*directory.relative_to(root).parts[:i])
                for i in range(1, len(directory.relative_to(root).parts))
            ),
        ]
        for ancestor in ancestors if directory != root else []:
            ignore = ancestor / ".gitignore"
            if ignore.is_file():
                try:
                    _, ignore_text = _read(checked_path(root, str(ignore)))
                    inherited_rules.append(
                        (ancestor, GitIgnoreSpec.from_lines(ignore_text.splitlines()))
                    )
                except (OSError, ValueError):
                    skipped += 1
    # 每个待扫描目录携带从父目录继承的忽略规则，使同名模式在正确的目录范围内生效。
    stack: list[tuple[Path, list[tuple[Path, GitIgnoreSpec]]]] = [(directory, inherited_rules)]
    while stack:
        folder, inherited = stack.pop()
        rules = list(inherited)
        ignore = folder / ".gitignore"
        if ignore.is_file() and not include:
            try:
                _, ignore_text = _read(checked_path(root, str(ignore)))
                rules.append((folder, GitIgnoreSpec.from_lines(ignore_text.splitlines())))
            except (OSError, ValueError):
                skipped += 1
        try:
            entries = os.scandir(folder)
        except OSError:
            skipped += 1
            continue
        with entries:
            for entry in entries:
                scanned += 1
                if scanned > 10000 or time.monotonic() > deadline or len(output) >= limit:
                    truncated = True
                    break
                path = Path(entry.path)
                is_directory = entry.is_dir(follow_symlinks=False)
                try:
                    checked_path(root, str(path), directory=is_directory)
                    ignored = False
                    for base, spec in rules:
                        match = spec.check_file(
                            path.relative_to(base).as_posix() + ("/" if is_directory else "")
                        )
                        if match.include is not None:
                            ignored = match.include
                    if entry.name == ".git" or (not include and (entry.name in _SKIP or ignored)):
                        continue
                    if is_directory:
                        stack.append((path, rules))
                        continue
                    relative = path.relative_to(root).as_posix()
                    if pattern not in {"*", "**/*"} and not Path(relative).match(pattern):
                        continue
                    if not search:
                        size = len(json.dumps(relative, ensure_ascii=False).encode("utf-8")) + 2
                        if output_bytes + size > MAX_OUTPUT:
                            truncated = True
                            break
                        output_bytes += size
                        output.append(relative)
                        continue
                    _, text = _read(path)
                    lines = text.splitlines()
                    for index, line in enumerate(lines):
                        if query not in line:
                            continue
                        excerpt = "\n".join(lines[max(0, index - context) : index + context + 1])
                        if (
                            output_bytes + len(excerpt.encode("utf-8")) > MAX_OUTPUT
                            or len(output) >= limit
                        ):
                            truncated = True
                            break
                        output_bytes += len(excerpt.encode("utf-8"))
                        output.append({"path": relative, "line": index + 1, "content": excerpt})
                except (OSError, ValueError):
                    skipped += 1
                if truncated:
                    break
        if truncated:
            break
    return ToolOutcome(
        data={
            "matches" if search else "paths": output,
            "truncated": truncated,
            "scanned": scanned,
            "skipped": skipped,
            "include_ignored": include,
        }
    )


def create_file_tools(workspace: Path) -> tuple[ToolDefinition, ...]:
    """为一个工作区装配文件查找、搜索、读取、写入和编辑工具，绑定各自的参数约定和执行函数。"""
    root = workspace.resolve(strict=True)
    path_schema: JsonObject = {"type": "string", "minLength": 1, "maxLength": 4096}
    scan: JsonObject = {
        "directory": path_schema,
        "pattern": path_schema,
        "include_ignored": {"type": "boolean"},
        "limit": {"type": "integer", "minimum": 1, "maximum": 100},
    }
    scan_result: JsonObject = {
        "truncated": {"type": "boolean"},
        "scanned": {"type": "integer", "minimum": 0},
        "skipped": {"type": "integer", "minimum": 0},
        "include_ignored": {"type": "boolean"},
    }
    mutation_result = object_schema(
        {
            "revision": path_schema,
            "previous_revision": path_schema,
            "bytes_written": {"type": "integer", "minimum": 0},
        },
        ["revision", "previous_revision", "bytes_written"],
    )
    results: dict[str, JsonObject] = {
        "find_files": object_schema(
            {
                **scan_result,
                "paths": {
                    "type": "array",
                    "items": {"type": "string"},
                    "maxItems": 100,
                },
            },
            [*scan_result, "paths"],
        ),
        "search_text": object_schema(
            {
                **scan_result,
                "matches": {
                    "type": "array",
                    "maxItems": 100,
                    "items": object_schema(
                        {
                            "path": path_schema,
                            "line": {"type": "integer", "minimum": 1},
                            "content": {"type": "string"},
                        },
                        ["path", "line", "content"],
                    ),
                },
            },
            [*scan_result, "matches"],
        ),
        "read_file": object_schema(
            {
                "content": {"type": "string"},
                "revision": path_schema,
                "start_line": {"type": "integer", "minimum": 1},
                "next_line": {"type": ["integer", "null"], "minimum": 1},
                "encoding": {"enum": ["utf-8", "utf-8-sig"]},
            },
            ["content", "revision", "start_line", "next_line", "encoding"],
        ),
        "create_file": mutation_result,
        "edit_file": mutation_result,
    }

    def make(
        name: str,
        properties: JsonObject,
        required: list[JsonValue],
        risk: RiskLevel,
        operation: Callable[[JsonObject], ToolOutcome],
        description: str,
    ) -> ToolDefinition:
        """为单个文件操作装配参数检查和执行函数，让不同的文件工具共用路径校验和取消处理。"""

        async def handler(args: JsonObject, context: ToolExecutionContext) -> ToolOutcome:
            del context

            def perform() -> ToolOutcome:
                if "path" in args:
                    path = checked_path(root, require_string(args, "path"))
                    with locked_ancestors(root, path):
                        return operation(args)
                return operation(args)

            task = asyncio.create_task(asyncio.to_thread(perform))
            try:
                return await asyncio.shield(task)
            except asyncio.CancelledError:
                return replace(await task, cancelled=True)
            except FileToolError as error:
                return ToolOutcome(completed=False, error_code=error.code, message=str(error))
            except (FileExistsError, FileNotFoundError, PermissionError) as error:
                return ToolOutcome(
                    completed=False,
                    error_code=ToolErrorCode.CONFLICT,
                    message=f"{type(error).__name__}: target unavailable",
                )

        def preflight(args: JsonObject, context: ToolExecutionContext) -> JsonObject:
            del context
            if len(json.dumps(args, ensure_ascii=False).encode("utf-8")) > MAX_REQUEST:
                raise ValueError("Tool request exceeds 512 KiB")
            prepared = dict(args)
            if "path" in args:
                prepared["path"] = str(checked_path(root, require_string(args, "path")))
            if "directory" in args:
                checked_path(root, require_string(args, "directory"), directory=True)
            return prepared

        return ToolDefinition(
            name,
            description,
            object_schema(properties, required),
            risk,
            handler,
            preflight,
            result_schema=results[name],
            manages_timeout=True,
        )

    return (
        make(
            "find_files",
            scan,
            [],
            RiskLevel.READ,
            lambda a: _scan(root, a, search=False),
            "Find source files. Paths relative to workspace; defaults respect .gitignore. "
            "Bounded scan reports truncation.",
        ),
        make(
            "read_file",
            {
                "path": path_schema,
                "start_line": {"type": "integer", "minimum": 1},
                "line_count": {"type": "integer", "minimum": 1, "maximum": 200},
            },
            ["path"],
            RiskLevel.READ,
            lambda a: _read_result(checked_path(root, require_string(a, "path")), a),
            "Read up to 200 UTF-8 lines (1-based) and obtain a full-file revision "
            "required by edit_file. Max file 2 MiB.",
        ),
        make(
            "search_text",
            {
                **scan,
                "query": {"type": "string", "minLength": 1, "maxLength": 1000},
                "context_lines": {"type": "integer", "minimum": 0, "maximum": 10},
            },
            ["query"],
            RiskLevel.READ,
            lambda a: _scan(root, a, search=True),
            "Literal source search with line numbers, context and explicit truncation; no regex.",
        ),
        make(
            "create_file",
            {"path": path_schema, "content": {"type": "string", "maxLength": MAX_REQUEST}},
            ["path", "content"],
            RiskLevel.WRITE,
            lambda a: _mutate(checked_path(root, require_string(a, "path")), a, create=True),
            "Create a new UTF-8 file after approval; never overwrite. "
            "Parent directory must already exist.",
        ),
        make(
            "edit_file",
            {
                "path": path_schema,
                "revision": path_schema,
                "edits": {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": 100,
                    "items": object_schema(
                        {
                            "old_text": {
                                "type": "string",
                                "minLength": 1,
                                "maxLength": MAX_REQUEST,
                            },
                            "new_text": {"type": "string", "maxLength": MAX_REQUEST},
                        },
                        ["old_text", "new_text"],
                    ),
                },
            },
            ["path", "revision", "edits"],
            RiskLevel.WRITE,
            lambda a: _mutate(checked_path(root, require_string(a, "path")), a, create=False),
            "Edit one read revision with unique nonoverlapping old/new replacements. "
            "CRLF/LF match equivalently; other whitespace is exact. Conflicts require a new read.",
        ),
    )
