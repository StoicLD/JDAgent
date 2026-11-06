"""A real host process terminated at observable event or filesystem boundaries."""

import asyncio
import json
import os
import sys
from pathlib import Path
from unittest.mock import patch

from jdagent.adapters.fake import FakeApproval, FakeModelPort
from jdagent.composition import build_runtime
from jdagent.configuration import CliOverrides, resolve_configuration
from jdagent.data_paths import DataPaths
from jdagent.domain.events import (
    MCPInvocationFinishedPayload,
    MCPInvocationStartedPayload,
    MCPTaskRegisteredPayload,
    RuntimeEvent,
    ToolExecutionStartedPayload,
)
from jdagent.domain.json import normalize_json
from jdagent.domain.model import ResponseCompleted, TextDelta, ToolCallCompleted
from jdagent.domain.tools import ApprovalDecision, ToolCall


async def main() -> None:
    engine, cut, session_id, workspace_text = sys.argv[1:]
    workspace = Path(workspace_text)
    paths = DataPaths.for_workspace(
        workspace, config_root=workspace / "config", data_root=workspace / "data"
    )
    config = resolve_configuration(
        workspace,
        CliOverrides(provider="fake", runtime_engine=engine),
        data_paths=paths,
        environment={},
    )

    class CrashAtFact:
        async def observe(self, event: RuntimeEvent) -> None:
            payload = event.payload
            if (
                cut == "before_intent"
                and isinstance(payload, ToolExecutionStartedPayload)
                and payload.call_id == "remote"
                or cut == "after_intent"
                and isinstance(payload, MCPInvocationStartedPayload)
                and payload.call_id == "remote"
                or cut == "registered"
                and isinstance(payload, MCPTaskRegisteredPayload)
                or cut == "finished"
                and isinstance(payload, MCPInvocationFinishedPayload)
                and payload.result.call_id == "remote"
            ):
                os._exit(72)

    original_link = os.link

    def publish_object(source: Path, destination: Path) -> None:
        original_link(source, destination)
        try:
            content = normalize_json(json.loads(source.read_text(encoding="utf-8")))
        except (ValueError, OSError):
            return
        if not isinstance(content, dict):
            return
        if (
            cut == "unregistered_handle"
            and "task_ref" in content
            and "identity_digest" in content
            or cut == "unpublished_result"
            and content.get("resultType") == "complete"
            and "content" in content
        ):
            os._exit(72)

    model = FakeModelPort(
        scripts=(
            (
                ToolCallCompleted(ToolCall("find", "search_tools", {"query": "read"})),
                ResponseCompleted("tool_calls"),
            ),
            (
                ToolCallCompleted(ToolCall("remote", "mcp__docs__read", {})),
                ResponseCompleted("tool_calls"),
            ),
            (TextDelta("Unexpected normal completion."), ResponseCompleted("stop")),
        )
    )
    runtime = build_runtime(
        config,
        FakeApproval(ApprovalDecision.APPROVE),
        model_wrapper=lambda _: model,
        event_observers=(CrashAtFact(),),
    )
    try:
        with patch("os.link", side_effect=publish_object):
            await runtime.coordinator.send("Read the remote task", new_session_id=session_id)
    finally:
        await runtime.aclose()


if __name__ == "__main__":
    asyncio.run(main())
