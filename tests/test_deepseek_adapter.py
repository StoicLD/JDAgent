import asyncio
import json

import httpx

from jdagent.adapters.deepseek import DeepSeekModelPort
from jdagent.domain.model import (
    MessageRole,
    ModelErrorCategory,
    ModelFailed,
    ModelMessage,
    ModelRequest,
    ModelToolDefinition,
    ResponseCompleted,
    TextDelta,
    ToolCallCompleted,
    Usage,
    UsageReported,
)


def _sse(*chunks: object) -> bytes:
    lines = [f"data: {json.dumps(chunk)}\n\n" for chunk in chunks]
    lines.append("data: [DONE]\n\n")
    return "".join(lines).encode()


async def _events(port: DeepSeekModelPort, request: ModelRequest) -> list[object]:
    return [event async for event in port.stream(request)]


def test_deepseek_stream_maps_text_usage_and_completion() -> None:
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(
            200,
            content=_sse(
                {"choices": [{"delta": {"content": "Hello"}, "finish_reason": None}]},
                {
                    "choices": [{"delta": {"content": "!"}, "finish_reason": "stop"}],
                    "usage": {
                        "prompt_tokens": 4,
                        "completion_tokens": 2,
                        "prompt_cache_hit_tokens": 1,
                        "completion_tokens_details": {"reasoning_tokens": 0},
                    },
                },
            ),
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="https://test")
    port = DeepSeekModelPort("not-a-real-key", client=client)
    request = ModelRequest(
        model="deepseek-v4-flash",
        messages=(ModelMessage(MessageRole.USER, "Hi"),),
    )

    events = asyncio.run(_events(port, request))
    asyncio.run(client.aclose())

    assert events == [
        TextDelta("Hello"),
        TextDelta("!"),
        UsageReported(Usage(4, 2, cached_tokens=1)),
        ResponseCompleted("stop"),
    ]
    usage = events[2]
    assert isinstance(usage, UsageReported)
    assert usage.usage.input_tokens == 4
    assert usage.usage.cached_tokens == 1
    assert captured[0].url.path == "/chat/completions"
    payload = json.loads(captured[0].content)
    assert payload["stream_options"] == {"include_usage": True}


def test_deepseek_stream_assembles_tool_call_fragments() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(
            200,
            content=_sse(
                {
                    "choices": [
                        {
                            "delta": {
                                "tool_calls": [
                                    {
                                        "index": 0,
                                        "id": "call-1",
                                        "function": {
                                            "name": "calculator",
                                            "arguments": '{"expression":',
                                        },
                                    }
                                ]
                            },
                            "finish_reason": None,
                        }
                    ]
                },
                {
                    "choices": [
                        {
                            "delta": {
                                "tool_calls": [{"index": 0, "function": {"arguments": '"2 + 3"}'}}]
                            },
                            "finish_reason": "tool_calls",
                        }
                    ]
                },
            ),
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="https://test")
    port = DeepSeekModelPort("not-a-real-key", client=client)
    request = ModelRequest(
        model="deepseek-v4-flash",
        messages=(ModelMessage(MessageRole.USER, "calculate"),),
        tools=(
            ModelToolDefinition(
                "calculator",
                "Calculate.",
                {"type": "object", "properties": {"expression": {"type": "string"}}},
            ),
        ),
    )

    events = asyncio.run(_events(port, request))
    asyncio.run(client.aclose())

    completed = [event for event in events if isinstance(event, ToolCallCompleted)]
    assert completed[0].call.arguments == {"expression": "2 + 3"}
    assert events[-1] == ResponseCompleted("tool_calls")


def test_deepseek_http_error_is_safely_classified() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(401, content=b'{"error":{"message":"contains secret details"}}')

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="https://test")
    port = DeepSeekModelPort("not-a-real-key", client=client)
    request = ModelRequest(
        model="deepseek-v4-flash",
        messages=(ModelMessage(MessageRole.USER, "Hi"),),
    )

    events = asyncio.run(_events(port, request))
    asyncio.run(client.aclose())

    assert len(events) == 1
    failure = events[0]
    assert isinstance(failure, ModelFailed)
    assert failure.category is ModelErrorCategory.AUTHENTICATION
    assert "secret details" not in failure.message
    assert "not-a-real-key" not in repr(events)


def test_deepseek_invalid_tool_arguments_are_protocol_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(
            200,
            content=_sse(
                {
                    "choices": [
                        {
                            "delta": {
                                "tool_calls": [
                                    {
                                        "index": 0,
                                        "id": "call-1",
                                        "function": {
                                            "name": "calculator",
                                            "arguments": "not-json",
                                        },
                                    }
                                ]
                            },
                            "finish_reason": "tool_calls",
                        }
                    ]
                }
            ),
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="https://test")
    port = DeepSeekModelPort("not-a-real-key", client=client)
    request = ModelRequest(
        model="deepseek-v4-flash",
        messages=(ModelMessage(MessageRole.USER, "calculate"),),
    )

    events = asyncio.run(_events(port, request))
    asyncio.run(client.aclose())

    failure = events[-1]
    assert isinstance(failure, ModelFailed)
    assert failure.category is ModelErrorCategory.INVALID_RESPONSE
