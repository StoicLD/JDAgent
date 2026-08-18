import pytest

from jdagent.context import ContextBuilder, ContextLimitError
from jdagent.domain.events import (
    AssistantMessageCompletedPayload,
    RuntimeEvent,
    RuntimeEventType,
    RuntimePayload,
    SessionStartedPayload,
    ToolExecutionCompletedPayload,
    UserMessagePayload,
)
from jdagent.domain.json import JsonObject
from jdagent.domain.model import MessageRole, ModelCapabilities, SystemPart
from jdagent.domain.tools import (
    RiskLevel,
    ToolCall,
    ToolDefinition,
    ToolExecutionContext,
    ToolResult,
    ToolResultStatus,
)


async def _unused_handler(arguments: JsonObject, context: ToolExecutionContext) -> str:
    del arguments, context
    return "unused"


def _event(
    sequence: int,
    event_type: RuntimeEventType,
    payload: RuntimePayload,
) -> RuntimeEvent:
    return RuntimeEvent.create(
        session_id="session-1",
        turn_id=None if sequence == 1 else "turn-1",
        sequence=sequence,
        event_type=event_type,
        payload=payload,
    )


def test_context_builder_builds_request_from_session_projection() -> None:
    call = ToolCall(call_id="call-1", name="calculator", arguments={"expression": "2 + 3"})
    events = [
        _event(1, RuntimeEventType.SESSION_STARTED, SessionStartedPayload()),
        _event(2, RuntimeEventType.USER_MESSAGE, UserMessagePayload("calculate")),
        _event(
            3,
            RuntimeEventType.ASSISTANT_MESSAGE_COMPLETED,
            AssistantMessageCompletedPayload("", (call,)),
        ),
        _event(
            4,
            RuntimeEventType.TOOL_EXECUTION_COMPLETED,
            ToolExecutionCompletedPayload(
                ToolResult("call-1", "calculator", ToolResultStatus.SUCCESS, output="5")
            ),
        ),
    ]
    tool = ToolDefinition(
        name="calculator",
        description="Evaluate arithmetic.",
        input_schema={
            "type": "object",
            "properties": {"expression": {"type": "string"}},
            "required": ["expression"],
            "additionalProperties": False,
        },
        risk=RiskLevel.PURE,
        handler=_unused_handler,
    )
    builder = ContextBuilder(
        model="fake",
        system_parts=(SystemPart("You are concise.", "test"),),
        tools=(tool,),
        max_context_tokens=1000,
    )

    request = builder.build(events, ModelCapabilities(context_window=2000))

    assert [message.role for message in request.messages] == [
        MessageRole.USER,
        MessageRole.ASSISTANT,
        MessageRole.TOOL,
    ]
    assert request.messages[2].content == "5"
    assert request.messages[2].tool_call_id == "call-1"
    assert request.tools[0].name == "calculator"


def test_context_builder_rejects_hard_limit_without_dropping_history() -> None:
    events = [
        _event(1, RuntimeEventType.SESSION_STARTED, SessionStartedPayload()),
        _event(2, RuntimeEventType.USER_MESSAGE, UserMessagePayload("x" * 200)),
    ]
    builder = ContextBuilder(model="fake", max_context_tokens=10)

    try:
        builder.build(events, ModelCapabilities(context_window=100))
    except ContextLimitError as error:
        assert error.estimated_tokens > error.limit_tokens
    else:
        raise AssertionError("Expected ContextLimitError")

    assert len(events) == 2


def test_context_builder_counts_tool_call_arguments_toward_limit() -> None:
    call = ToolCall("call-1", "echo", {"text": "x" * 400})
    events = [
        _event(1, RuntimeEventType.SESSION_STARTED, SessionStartedPayload()),
        _event(2, RuntimeEventType.USER_MESSAGE, UserMessagePayload("echo")),
        _event(
            3,
            RuntimeEventType.ASSISTANT_MESSAGE_COMPLETED,
            AssistantMessageCompletedPayload("", (call,)),
        ),
    ]
    builder = ContextBuilder(model="fake", max_context_tokens=20)

    with pytest.raises(ContextLimitError):
        builder.build(events, ModelCapabilities())
