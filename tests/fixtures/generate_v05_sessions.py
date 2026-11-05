"""Generate frozen compatibility samples using an explicitly supplied v0.5 source tree."""

import argparse
import hashlib
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

BASE_REVISION = "6996e764537c12f27e85a060011ace32d359a5db"


def generate(source: Path, destination: Path) -> None:
    sys.path.insert(0, str(source.resolve()))
    from jdagent.adapters import jsonl_session
    from jdagent.adapters.jsonl_session import event_to_data
    from jdagent.domain.errors import StopReason
    from jdagent.domain.events import (
        AssistantMessageCompletedPayload,
        RuntimeEvent,
        RuntimeEventType,
        RuntimePayload,
        SessionStartedPayload,
        ToolExecutionCompletedPayload,
        ToolExecutionStartedPayload,
        TurnCompletedPayload,
        UserMessagePayload,
    )
    from jdagent.domain.tools import (
        RiskLevel,
        ToolCall,
        ToolErrorCode,
        ToolOutcome,
        ToolResult,
        ToolResultStatus,
    )

    assert Path(jsonl_session.__file__).resolve().is_relative_to(source.resolve())
    destination.mkdir(parents=True, exist_ok=True)
    hashes: dict[str, str] = {}
    for mode in ("complete", "safe", "unknown_started", "unknown_result"):
        identity = f"v05-{mode}"
        facts: list[tuple[RuntimeEventType, RuntimePayload]] = [
            (RuntimeEventType.SESSION_STARTED, SessionStartedPayload(identity, "fixture-v05")),
            (
                RuntimeEventType.USER_MESSAGE,
                UserMessagePayload("Never edit protected.txt. Create effect.txt once."),
            ),
        ]
        if mode != "safe":
            if mode.startswith("unknown"):
                facts.extend(
                    [
                        (
                            RuntimeEventType.TURN_COMPLETED,
                            TurnCompletedPayload(StopReason.COMPLETED, 1, 0),
                        ),
                        (
                            RuntimeEventType.USER_MESSAGE,
                            UserMessagePayload("Unconfirmed gap message: create effect.txt."),
                        ),
                    ]
                )
            call = ToolCall("old-write", "create_file", {"path": "effect.txt", "content": "once"})
            facts.extend(
                [
                    (
                        RuntimeEventType.ASSISTANT_MESSAGE_COMPLETED,
                        AssistantMessageCompletedPayload("", (call,)),
                    ),
                    (
                        RuntimeEventType.TOOL_EXECUTION_STARTED,
                        ToolExecutionStartedPayload(call.call_id, call.name, RiskLevel.WRITE),
                    ),
                ]
            )
            if mode != "unknown_started":
                unknown = mode == "unknown_result"
                outcome = ToolOutcome(
                    completed=not unknown, effect="unknown" if unknown else "committed"
                )
                result = ToolResult(
                    call.call_id,
                    call.name,
                    ToolResultStatus.ERROR if unknown else ToolResultStatus.SUCCESS,
                    output="Write outcome is unknown." if unknown else "Created effect.txt.",
                    error_code=ToolErrorCode.UNCERTAIN if unknown else None,
                    outcome=outcome,
                )
                facts.append(
                    (
                        RuntimeEventType.TOOL_EXECUTION_COMPLETED,
                        ToolExecutionCompletedPayload(result),
                    )
                )
        if mode == "complete":
            facts.append(
                (RuntimeEventType.TURN_COMPLETED, TurnCompletedPayload(StopReason.COMPLETED, 1, 1))
            )
        lines: list[str] = []
        for index, (kind, payload) in enumerate(facts, start=1):
            event = RuntimeEvent(
                4,
                f"{identity}-event-{index}",
                identity,
                None
                if index == 1
                else (
                    "interrupted-turn" if mode.startswith("unknown") and index > 3 else "old-turn"
                ),
                index,
                kind,
                datetime(2026, 10, 22, tzinfo=UTC),
                payload,
            )
            lines.append(json.dumps(event_to_data(event), sort_keys=True, ensure_ascii=False))
        raw = ("\n".join(lines) + "\n").encode()
        name = f"{identity}.jsonl"
        (destination / name).write_bytes(raw)
        hashes[name] = hashlib.sha256(raw).hexdigest()
    manifest = {
        "source_revision": BASE_REVISION,
        "schema_version": 4,
        "files": hashes,
        "source_files": {
            str(p.relative_to(source)).replace("\\", "/"): hashlib.sha256(
                p.read_bytes()
            ).hexdigest()
            for p in [
                source / "jdagent" / "adapters" / "jsonl_session.py",
                source / "jdagent" / "domain" / "events.py",
            ]
        },
    }
    (destination / "manifest.json").write_text(
        json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("source", type=Path)
    parser.add_argument("destination", type=Path)
    args = parser.parse_args()
    generate(args.source, args.destination)
