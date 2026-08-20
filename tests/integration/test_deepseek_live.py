import asyncio
import os
from pathlib import Path

import pytest

from jdagent.adapters.deepseek import DeepSeekModelPort
from jdagent.adapters.fake import FakeApproval
from jdagent.composition import load_deepseek_api_key
from jdagent.domain.model import (
    MessageRole,
    ModelEvent,
    ModelFailed,
    ModelMessage,
    ModelRequest,
    ModelToolDefinition,
    ResponseCompleted,
    TextDelta,
    ToolCallCompleted,
)
from jdagent.domain.tools import (
    ApprovalDecision,
    ToolExecutionContext,
    ToolResultStatus,
)
from jdagent.tools.builtins import create_builtin_tools
from jdagent.tools.permissions import DefaultPermissionPolicy
from jdagent.tools.runtime import ToolRegistry, ToolRuntime
from jdagent.tools.workspace import WorkspacePathResolver

pytestmark = pytest.mark.integration


def _live_configuration() -> tuple[str, str, str]:
    if os.environ.get("JDAGENT_RUN_DEEPSEEK_INTEGRATION") != "1":
        pytest.skip("Set JDAGENT_RUN_DEEPSEEK_INTEGRATION=1 for live provider tests")
    api_key = load_deepseek_api_key(os.environ.get("DEEPSEEK_API_KEY"))
    if not api_key:
        pytest.skip("A DeepSeek API key is required for live provider tests")
    return (
        api_key,
        os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com"),
        os.environ.get("DEEPSEEK_MODEL", "deepseek-v4-flash"),
    )


def test_real_deepseek_streams_text() -> None:
    api_key, base_url, model = _live_configuration()

    async def scenario() -> list[ModelEvent]:
        port = DeepSeekModelPort(api_key, base_url=base_url)
        try:
            request = ModelRequest(
                model=model,
                messages=(ModelMessage(MessageRole.USER, "Reply with exactly: JDAgent live"),),
                provider_options={"deepseek": {"thinking": {"type": "disabled"}}},
            )
            return [event async for event in port.stream(request)]
        finally:
            await port.aclose()

    events = asyncio.run(scenario())

    assert not any(isinstance(event, ModelFailed) for event in events)
    assert any(isinstance(event, TextDelta) for event in events)
    assert any(isinstance(event, ResponseCompleted) for event in events)


def test_real_deepseek_tool_call_round_trip(tmp_path: Path) -> None:
    api_key, base_url, model = _live_configuration()

    async def scenario() -> tuple[ToolResultStatus, list[ModelEvent], list[ModelEvent]]:
        port = DeepSeekModelPort(api_key, base_url=base_url)
        try:
            tool = create_builtin_tools(WorkspacePathResolver(tmp_path))[0]
            first_request = ModelRequest(
                model=model,
                messages=(ModelMessage(MessageRole.USER, "Use the calculator for 2 + 3."),),
                tools=(ModelToolDefinition(tool.name, tool.description, tool.input_schema),),
                provider_options={
                    "deepseek": {
                        "thinking": {"type": "disabled"},
                        "tool_choice": {
                            "type": "function",
                            "function": {"name": "calculator"},
                        },
                    }
                },
            )
            first_events = [event async for event in port.stream(first_request)]
            call_event = next(
                event for event in first_events if isinstance(event, ToolCallCompleted)
            )
            runtime = ToolRuntime(
                ToolRegistry((tool,)),
                DefaultPermissionPolicy(),
                FakeApproval(ApprovalDecision.APPROVE),
            )
            result = await runtime.execute(
                call_event.call,
                ToolExecutionContext("live", "turn", tmp_path),
            )
            second_request = ModelRequest(
                model=model,
                messages=(
                    ModelMessage(MessageRole.USER, "Use the calculator for 2 + 3."),
                    ModelMessage(MessageRole.ASSISTANT, "", tool_calls=(call_event.call,)),
                    ModelMessage(
                        MessageRole.TOOL,
                        result.output,
                        tool_call_id=call_event.call.call_id,
                        name=call_event.call.name,
                    ),
                ),
                tools=(ModelToolDefinition(tool.name, tool.description, tool.input_schema),),
                provider_options={
                    "deepseek": {
                        "thinking": {"type": "disabled"},
                        "tool_choice": "none",
                    }
                },
            )
            second_events = [event async for event in port.stream(second_request)]
            return result.status, first_events, second_events
        finally:
            await port.aclose()

    result_status, first_events, events = asyncio.run(scenario())

    assert result_status is ToolResultStatus.SUCCESS
    assert not any(isinstance(event, ModelFailed) for event in first_events)
    assert not any(isinstance(event, ModelFailed) for event in events)
    assert any(isinstance(event, TextDelta) for event in events)
    assert any(isinstance(event, ResponseCompleted) for event in events)
