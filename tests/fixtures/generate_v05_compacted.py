"""Produce a deterministic compacted session with a supplied frozen v0.5 runtime."""

import argparse
import asyncio
import hashlib
import itertools
import json
import sys
from collections.abc import AsyncIterator
from datetime import UTC, datetime, tzinfo
from pathlib import Path
from unittest.mock import patch
from uuid import UUID


class FixedDatetime(datetime):
    @classmethod
    def now(cls, tz: tzinfo | None = None) -> datetime:
        return datetime(2026, 10, 22, tzinfo=UTC).astimezone(tz)


async def generate(source: Path, destination: Path) -> None:
    source = await asyncio.to_thread(source.resolve)
    sys.path.insert(0, str(source))
    import jdagent.context_runtime as context_module
    from jdagent.adapters.context_objects import FileContextObjects
    from jdagent.adapters.fake import FakeToolRuntime
    from jdagent.adapters.jsonl_session import event_to_data
    from jdagent.adapters.memory import InMemorySession
    from jdagent.context import ContextBuilder
    from jdagent.context_budget import ContextBudget
    from jdagent.context_runtime import ContextService
    from jdagent.core.loop import AgentLoop
    from jdagent.domain.errors import StopReason
    from jdagent.domain.events import (
        AssistantMessageCompletedPayload,
        CompactionCommittedPayload,
        ModelRequestPreparedPayload,
        RuntimeEventType,
        SessionStartedPayload,
        ToolExecutionCompletedPayload,
        UserMessagePayload,
    )
    from jdagent.domain.model import (
        ModelCapabilities,
        ModelEvent,
        ModelRequest,
        ModelSettings,
        ResponseCompleted,
        TextDelta,
    )
    from jdagent.domain.tools import ToolCall, ToolExecutionContext, ToolResult, ToolResultStatus
    from jdagent.eventing import EventJournal

    imported = await asyncio.to_thread(Path(context_module.__file__).resolve)
    assert imported.is_relative_to(source)
    await asyncio.to_thread(destination.mkdir, parents=True, exist_ok=True)
    serial = itertools.count(1)

    class SummaryModel:
        capabilities = ModelCapabilities(context_window=64000)

        async def stream(self, request: ModelRequest) -> AsyncIterator[ModelEvent]:
            if request.metadata.get("call_kind") == "compaction":
                yield TextDelta(
                    json.dumps(
                        {
                            "goal": "Legacy compacted goal",
                            "facts": [
                                {"text": "Report read", "status": "completed", "sources": [4]}
                            ],
                            "pending": [],
                            "decisions": [],
                            "paths": ["protected.txt"],
                        }
                    )
                )
            else:
                yield TextDelta("Legacy turn completed.")
            yield ResponseCompleted("stop")

    with (
        patch("jdagent.domain.events.uuid4", side_effect=lambda: UUID(int=next(serial))),
        patch("jdagent.context_runtime.uuid4", side_effect=lambda: UUID(int=next(serial))),
        patch("jdagent.domain.events.datetime", FixedDatetime),
    ):
        journal = await EventJournal.open(InMemorySession(), "v05-compacted")
        await journal.record(
            None,
            RuntimeEventType.SESSION_STARTED,
            SessionStartedPayload("compacted", "fixture-v05"),
        )
        await journal.record(
            "old",
            RuntimeEventType.USER_MESSAGE,
            UserMessagePayload("Never edit protected.txt. Inspect the report."),
        )
        await journal.record(
            "old",
            RuntimeEventType.ASSISTANT_MESSAGE_COMPLETED,
            AssistantMessageCompletedPayload(
                "", (ToolCall("old-read", "read_file", {"path": "report.txt"}),)
            ),
        )
        await journal.record(
            "old",
            RuntimeEventType.TOOL_EXECUTION_COMPLETED,
            ToolExecutionCompletedPayload(
                ToolResult(
                    "old-read",
                    "read_file",
                    ToolResultStatus.SUCCESS,
                    output="legacy-report-original " * 200,
                )
            ),
        )
        model = SummaryModel()
        builder = ContextBuilder(model="fake", settings=ModelSettings(max_output_tokens=1024))
        service = ContextService(
            builder=builder,
            model=model,
            journal=journal,
            objects=FileContextObjects(destination / "objects"),
            budget=ContextBudget(64000, output_tokens=1024),
        )
        service.request_compaction()
        loop = AgentLoop(model, builder, FakeToolRuntime(()), journal, context_service=service)
        result = await loop.run("old", ToolExecutionContext("v05-compacted", "old", destination))
        assert result.stop_reason is StopReason.COMPLETED
        commits = [
            e.payload for e in journal.events if isinstance(e.payload, CompactionCommittedPayload)
        ]
        assert len(commits) == 1
        manifests = [
            e.payload
            for e in journal.events
            if isinstance(e.payload, ModelRequestPreparedPayload) and e.payload.call_kind == "task"
        ]
        assert len(manifests) == 1
        raw = (
            "\n".join(json.dumps(event_to_data(e), sort_keys=True) for e in journal.events) + "\n"
        ).encode()
        await asyncio.to_thread((destination / "v05-compacted.jsonl").write_bytes, raw)

    def publish_manifest() -> None:
        metadata = {
            "source_revision": "6996e764537c12f27e85a060011ace32d359a5db",
            "checkpoint": commits[0].object_hash,
            "request_manifest": manifests[0].object_hash,
            "files": {
                str(p.relative_to(destination)).replace("\\", "/"): hashlib.sha256(
                    p.read_bytes()
                ).hexdigest()
                for p in [
                    destination / "v05-compacted.jsonl",
                    *sorted((destination / "objects").iterdir()),
                ]
            },
            "source_files": {
                str(p.relative_to(source)).replace("\\", "/"): hashlib.sha256(
                    p.read_bytes()
                ).hexdigest()
                for p in [
                    source / "jdagent" / name
                    for name in ("context_runtime.py", "context_archive.py", "core/loop.py")
                ]
            },
        }
        (destination / "manifest.json").write_text(
            json.dumps(metadata, indent=2) + "\n", encoding="utf-8", newline="\n"
        )

    await asyncio.to_thread(publish_manifest)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("source", type=Path)
    parser.add_argument("destination", type=Path)
    args = parser.parse_args()
    asyncio.run(generate(args.source, args.destination))
