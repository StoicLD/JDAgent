"""Process fixture that exits only after selected durable session facts."""

import argparse
import asyncio
import os
from pathlib import Path

from jdagent.adapters.jsonl_session import JsonlSession
from jdagent.domain.events import (
    RuntimeEventType,
    SessionStartedPayload,
    ToolExecutionStartedPayload,
    UserMessagePayload,
)
from jdagent.domain.tools import RiskLevel
from jdagent.eventing import EventJournal


async def _write(directory: Path, mode: str) -> None:
    session = JsonlSession(directory)
    journal = await EventJournal.open(session, "forced-session")
    await journal.record(
        None,
        RuntimeEventType.SESSION_STARTED,
        SessionStartedPayload("forced", "workspace-1"),
    )
    await journal.record(
        "turn-1",
        RuntimeEventType.USER_MESSAGE,
        UserMessagePayload("durable prompt"),
    )
    if mode == "write-started":
        await journal.record(
            "turn-1",
            RuntimeEventType.TOOL_EXECUTION_STARTED,
            ToolExecutionStartedPayload("call-1", "write_text_file", RiskLevel.WRITE),
        )
    os._exit(23)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("directory", type=Path)
    parser.add_argument("mode", choices=("user-only", "write-started"))
    arguments = parser.parse_args()
    asyncio.run(_write(arguments.directory, arguments.mode))


if __name__ == "__main__":
    main()
