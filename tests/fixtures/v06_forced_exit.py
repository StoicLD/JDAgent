"""Terminate a real composed runtime after a chosen durable fact, without cleanup."""

import argparse
import asyncio
import os
from pathlib import Path

import httpx
import pytest

from jdagent.domain.events import RuntimeEvent, RuntimeEventType
from jdagent.domain.tools import ToolCall
from tests.runtime_matrix import RuntimeCase, model_response

PHASES = {
    "before_request": RuntimeEventType.USER_MESSAGE,
    "write_started": RuntimeEventType.TOOL_EXECUTION_STARTED,
    "result_saved": RuntimeEventType.TOOL_EXECUTION_COMPLETED,
    "turn_completed": RuntimeEventType.TURN_COMPLETED,
}


class ExitAfterFact:
    def __init__(self, phase: str) -> None:
        self.kind = PHASES[phase]

    async def observe(self, event: RuntimeEvent) -> None:
        if event.event_type is self.kind:
            os._exit(23)


async def run(root: Path, engine: str, adapter: str, phase: str) -> None:
    count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal count
        del request
        count += 1
        if count == 1:
            return model_response(
                "",
                (ToolCall("write-once", "create_file", {"path": "effect.txt", "content": "once"}),),
            )
        return model_response("Created once.")

    with pytest.MonkeyPatch.context() as patch:
        case = RuntimeCase(root, patch, (engine, adapter))
        async with case.open(handler, observers=(ExitAfterFact(phase),)) as runtime:
            await runtime.coordinator.send(
                "Create effect.txt exactly once.", new_session_id="crashed"
            )
    raise AssertionError("The selected exit boundary was never reached")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("root", type=Path)
    parser.add_argument("engine", choices=("native", "langgraph"))
    parser.add_argument("adapter", choices=("native", "langchain"))
    parser.add_argument("phase", choices=tuple(PHASES))
    args = parser.parse_args()
    asyncio.run(run(args.root, args.engine, args.adapter, args.phase))
