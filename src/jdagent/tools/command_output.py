"""Session-owned bounded output logs; capacity is serialized across processes."""

import json
import re
import threading
from pathlib import Path
from typing import BinaryIO
from uuid import uuid4

from jdagent.adapters.windows_files import locked_file
from jdagent.domain.json import JsonObject, normalize_json, require_object
from jdagent.domain.tools import command_log_id


def _component(value: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", value):
        raise ValueError("Invalid log/session identifier")
    return value


class CommandLog:
    """One invocation's two streams with independent preview and storage limits."""

    def __init__(self, folder: Path, capacity: int, call_id: str = "") -> None:
        self.log_id = command_log_id(call_id) if call_id else uuid4().hex
        self._folder = folder / self.log_id
        self._folder.mkdir(parents=True)
        self._capacity = capacity
        self._lock = threading.Lock()
        self._saved = 0
        self._previewed = 0
        self._sizes = {"stdout": 0, "stderr": 0}
        self._kept = {"stdout": 0, "stderr": 0}
        self._previews = {"stdout": bytearray(), "stderr": bytearray()}

    @property
    def status_file(self) -> Path:
        return self._folder / "process.json"

    def drain(self, stream: str, pipe: BinaryIO) -> None:
        with (self._folder / stream).open("xb") as saved:
            while chunk := pipe.read(8192):
                with self._lock:
                    self._sizes[stream] += len(chunk)
                    preview = chunk[: max(0, 16384 - self._previewed)]
                    self._previews[stream].extend(preview)
                    self._previewed += len(preview)
                    retained = chunk[: max(0, self._capacity - self._saved)]
                    saved.write(retained)
                    self._saved += len(retained)
                    self._kept[stream] += len(retained)

    def finish(self) -> JsonObject:
        result: JsonObject = {
            "log_id": self.log_id,
            "streams": {},
            "preview_truncated": sum(self._sizes.values()) > self._previewed,
            "log_loss": sum(self._sizes.values()) > self._saved,
        }
        streams: JsonObject = {}
        for name in ("stdout", "stderr"):
            raw = bytes(self._previews[name])
            text = raw.decode("utf-8", errors="replace")
            streams[name] = {
                "preview": text,
                "total_bytes": self._sizes[name],
                "saved_bytes": self._kept[name],
                "replacement_characters": "\ufffd" in text,
            }
        result["streams"] = streams
        (self._folder / "metadata.json").write_text(json.dumps(result), encoding="utf-8")
        return result


class CommandLogs:
    """Resolve opaque IDs only inside one workspace's session log partition."""

    def __init__(
        self,
        root: Path,
        *,
        total_limit: int = 100 * 1024 * 1024,
        call_limit: int = 10 * 1024 * 1024,
    ) -> None:
        self.root = root
        self.total_limit = total_limit
        self.call_limit = call_limit

    def start(self, session_id: str, call_id: str = "") -> CommandLog:
        self.root.mkdir(parents=True, exist_ok=True)
        used = sum(
            path.stat().st_size
            for path in self.root.glob("*/*/*")
            if path.name in {"stdout", "stderr"}
        )
        available = min(self.call_limit, self.total_limit - used)
        if available <= 0:
            raise OSError("Command output capacity exhausted; clear logs before running commands")
        return CommandLog(self.root / _component(session_id), available, call_id)

    def read(
        self, session_id: str, log_id: str, stream: str, offset: int, length: int
    ) -> JsonObject:
        folder = self.root / _component(session_id) / _component(log_id)
        if stream not in {"stdout", "stderr"} or offset < 0 or not 1 <= length <= 32768:
            raise ValueError("Invalid stream range")
        if (folder / "cleared").exists():
            return {"log_id": log_id, "state": "cleared"}
        metadata: JsonObject
        if (folder / "metadata.json").exists():
            metadata = require_object(
                normalize_json(json.loads((folder / "metadata.json").read_text(encoding="utf-8"))),
                "metadata",
            )
        else:
            metadata = {"state": "incomplete", "log_loss": True}
        with (folder / stream).open("rb") as source:
            source.seek(offset)
            raw = source.read(length)
        return {
            "log_id": log_id,
            "stream": stream,
            "offset": offset,
            "content": raw.decode("utf-8", errors="replace"),
            "next_offset": offset + len(raw),
            "metadata": metadata,
        }

    def clear(self, session_id: str) -> None:
        folder = self.root / _component(session_id)
        if not folder.exists():
            return
        lock_path = self.root / "capacity.lock"
        lock_path.touch(exist_ok=True)
        with locked_file(lock_path, write=True):
            for log in folder.iterdir():
                if not log.is_dir() or log.is_symlink():
                    continue
                for stream in ("stdout", "stderr"):
                    (log / stream).unlink(missing_ok=True)
                (log / "cleared").touch()
