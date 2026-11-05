import asyncio
import json
from collections.abc import AsyncIterator
from dataclasses import replace

import httpx
import pytest

from jdagent.adapters.langchain_model import LangChainModelPort
from jdagent.domain.json import JsonObject
from jdagent.domain.model import (
    MessageRole,
    ModelErrorCategory,
    ModelEvent,
    ModelFailed,
    ModelMessage,
    ModelRequest,
    ModelSettings,
    ModelToolDefinition,
    ResponseCompleted,
    SystemPart,
    TextDelta,
    ToolCallCompleted,
    Usage,
    UsageReported,
)


def _chunk(delta: JsonObject, finish: str | None = None) -> JsonObject:
    return {
        "id": "response-1",
        "object": "chat.completion.chunk",
        "created": 1,
        "model": "deepseek-test",
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
    }


def _sse(*chunks: JsonObject) -> bytes:
    return (
        "".join(f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n" for chunk in chunks)
        + "data: [DONE]\n\n"
    ).encode()


def _request() -> ModelRequest:
    return ModelRequest(
        model="deepseek-test",
        messages=(ModelMessage(MessageRole.USER, "hello"),),
    )


async def _collect(stream: AsyncIterator[ModelEvent]) -> list[ModelEvent]:
    return [event async for event in stream]


def test_model_settings_are_taken_from_each_request_and_borrowed_client_stays_open() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, content=_sse(_chunk({"content": "hello"}, "stop")))

    async def scenario() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            port = LangChainModelPort("dummy-key", base_url="https://test/v1", client=client)
            for name, timeout, maximum in (("model-a", 4.0, 32), ("model-b", 9.0, 64)):
                request = replace(
                    _request(),
                    model=name,
                    system_parts=(SystemPart("first"), SystemPart("second")),
                    settings=ModelSettings(timeout_seconds=timeout, max_output_tokens=maximum),
                    provider_options={"deepseek": {"top_p": 0.8}},
                )
                assert await _collect(port.stream(request)) == [
                    TextDelta("hello"),
                    ResponseCompleted("stop"),
                ]
            await port.aclose()
            await port.aclose()
            assert not client.is_closed
            closed = await _collect(port.stream(_request()))
            assert isinstance(closed[0], ModelFailed)

    asyncio.run(scenario())
    assert len(requests) == 2
    for captured, expected in zip(
        requests, (("model-a", 4.0, 32), ("model-b", 9.0, 64)), strict=True
    ):
        body = json.loads(captured.content)
        assert captured.url == "https://test/v1/chat/completions"
        assert body["model"] == expected[0]
        assert captured.extensions["timeout"]["read"] == expected[1]
        assert body["max_tokens"] == expected[2]
        assert body["thinking"] == {"type": "disabled"}
        assert body["top_p"] == 0.8
        assert body["stream_options"] == {"include_usage": True}
        assert body["messages"] == [
            {"role": "system", "content": "first"},
            {"role": "system", "content": "second"},
            {"role": "user", "content": "hello"},
        ]


def test_complete_tool_json_and_late_cumulative_usage_are_validated_once() -> None:
    usage: JsonObject = {
        "choices": [],
        "usage": {
            "prompt_tokens": 12,
            "completion_tokens": 4,
            "total_tokens": 16,
            "prompt_cache_hit_tokens": 7,
        },
    }
    body = _sse(
        _chunk(
            {
                "tool_calls": [
                    {
                        "index": 0,
                        "id": "call-1",
                        "type": "function",
                        "function": {"name": "echo", "arguments": '{"text":'},
                    }
                ]
            }
        ),
        _chunk({"tool_calls": [{"index": 0, "function": {"arguments": '"中文"}'}}]}, "tool_calls"),
        usage,
        usage,
    )

    async def scenario() -> list[ModelEvent]:
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(lambda _: httpx.Response(200, content=body))
        ) as client:
            port = LangChainModelPort("dummy-key", base_url="https://test", client=client)
            try:
                return await _collect(port.stream(_request()))
            finally:
                await port.aclose()

    events = asyncio.run(scenario())
    assert [event.usage for event in events if isinstance(event, UsageReported)] == [
        Usage(12, 4, 7)
    ]
    completed = [event.call for event in events if isinstance(event, ToolCallCompleted)]
    assert len(completed) == 1 and completed[0].arguments == {"text": "中文"}
    assert events[-1] == ResponseCompleted("tool_calls")


@pytest.mark.parametrize("arguments", ['{"unfinished":', "[1,2]", "not-json"])
def test_invalid_complete_arguments_never_publish_an_executable_call(arguments: str) -> None:
    body = _sse(
        _chunk(
            {
                "tool_calls": [
                    {
                        "index": 0,
                        "id": "one",
                        "type": "function",
                        "function": {"name": "echo", "arguments": arguments},
                    }
                ]
            },
            "tool_calls",
        )
    )
    events = _response_events(body)
    assert isinstance(events[-1], ModelFailed)
    assert events[-1].category is ModelErrorCategory.INVALID_RESPONSE
    assert not any(isinstance(event, (ToolCallCompleted, ResponseCompleted)) for event in events)


def _response_events(body: bytes, *, json_mode: bool = False) -> list[ModelEvent]:
    async def scenario() -> list[ModelEvent]:
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(lambda _: httpx.Response(200, content=body))
        ) as client:
            port = LangChainModelPort("dummy-key", base_url="https://test", client=client)
            try:
                request = _request()
                if json_mode:
                    request = replace(
                        request,
                        provider_options={"deepseek": {"response_format": {"type": "json_object"}}},
                    )
                return await _collect(port.stream(request))
            finally:
                await port.aclose()

    return asyncio.run(scenario())


@pytest.mark.parametrize("finish", ["empty", "length", "content_filter"])
def test_incomplete_json_mode_is_a_model_failure_without_sdk_exception(finish: str) -> None:
    body = (
        _sse() if finish == "empty" else _sse(_chunk({"role": "assistant", "content": "{"}, finish))
    )
    events = _response_events(body, json_mode=True)
    assert isinstance(events[-1], ModelFailed)
    assert events[-1].category is ModelErrorCategory.INVALID_RESPONSE
    assert not any(isinstance(event, (ToolCallCompleted, ResponseCompleted)) for event in events)


@pytest.mark.parametrize(
    "chunks",
    [
        [_chunk({"content": "unfinished"})],
        [_chunk({}, "stop"), _chunk({}, "stop")],
        [_chunk({}, "tool_calls")],
        [
            _chunk(
                {"tool_calls": [{"index": 0, "function": {"name": "echo", "arguments": "{}"}}]},
                "tool_calls",
            )
        ],
        [
            _chunk(
                {
                    "tool_calls": [
                        {"index": i, "id": "same", "function": {"name": "echo", "arguments": "{}"}}
                        for i in range(2)
                    ]
                },
                "tool_calls",
            )
        ],
    ],
)
def test_missing_or_duplicate_protocol_evidence_is_rejected(chunks: list[JsonObject]) -> None:
    events = _response_events(_sse(*chunks))
    assert isinstance(events[-1], ModelFailed)
    assert not any(isinstance(event, (ToolCallCompleted, ResponseCompleted)) for event in events)


@pytest.mark.parametrize(
    "status,category",
    [
        (401, ModelErrorCategory.AUTHENTICATION),
        (429, ModelErrorCategory.RATE_LIMIT),
        (500, ModelErrorCategory.PROVIDER_INTERNAL),
    ],
)
def test_sdk_does_not_retry_and_failure_does_not_leak_response_body(
    status: int, category: ModelErrorCategory
) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(status, json={"error": {"message": "private secret payload"}})

    async def scenario() -> list[ModelEvent]:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            port = LangChainModelPort("dummy-key", base_url="https://test", client=client)
            try:
                return await _collect(port.stream(_request()))
            finally:
                await port.aclose()

    events = asyncio.run(scenario())
    assert len(requests) == 1
    assert isinstance(events[-1], ModelFailed) and events[-1].category is category
    assert "private secret payload" not in repr(events) and "dummy-key" not in repr(events)


def test_maintenance_json_mode_sends_one_request_without_adding_tools() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200, content=_sse(_chunk({"role": "assistant", "content": '{"ok":true}'}, "stop"))
        )

    async def scenario() -> list[ModelEvent]:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            port = LangChainModelPort("dummy-key", base_url="https://test", client=client)
            try:
                request = replace(
                    _request(),
                    provider_options={"deepseek": {"response_format": {"type": "json_object"}}},
                )
                return await _collect(port.stream(request))
            finally:
                await port.aclose()

    events = asyncio.run(scenario())
    assert len(requests) == 1
    body = json.loads(requests[0].content)
    assert body["response_format"] == {"type": "json_object"} and "tools" not in body
    assert not any(isinstance(event, ModelFailed) for event in events)
    assert "".join(event.text for event in events if isinstance(event, TextDelta)) == '{"ok":true}'
    assert sum(isinstance(event, ResponseCompleted) for event in events) == 1


def test_tool_binding_preserves_project_schema() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, content=_sse(_chunk({"content": "done"}, "stop")))

    async def scenario() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            port = LangChainModelPort("dummy-key", base_url="https://test", client=client)
            try:
                request = replace(
                    _request(),
                    tools=(
                        ModelToolDefinition(
                            "echo",
                            "Echo text",
                            {"type": "object", "properties": {"text": {"type": "string"}}},
                        ),
                    ),
                )
                await _collect(port.stream(request))
            finally:
                await port.aclose()

    asyncio.run(scenario())
    body = json.loads(requests[0].content)
    assert body["tool_choice"] == "auto"
    assert body["tools"] == [
        {
            "type": "function",
            "function": {
                "name": "echo",
                "description": "Echo text",
                "parameters": {"type": "object", "properties": {"text": {"type": "string"}}},
            },
        }
    ]


def test_cancellation_closes_in_flight_stream_and_propagates() -> None:
    async def scenario() -> None:
        entered = asyncio.Event()
        closed = asyncio.Event()

        class PendingResponse(httpx.AsyncByteStream):
            async def __aiter__(self) -> AsyncIterator[bytes]:
                entered.set()
                await asyncio.Event().wait()
                yield b""

            async def aclose(self) -> None:
                closed.set()

        response_stream = PendingResponse()
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(lambda _: httpx.Response(200, stream=response_stream))
        ) as client:
            port = LangChainModelPort("dummy-key", base_url="https://test", client=client)
            task = asyncio.create_task(_collect(port.stream(_request())))
            try:
                await asyncio.wait_for(entered.wait(), 2)
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
                await asyncio.wait_for(closed.wait(), 2)
                assert not client.is_closed
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                await port.aclose()

    asyncio.run(scenario())


@pytest.mark.parametrize("timeout", [True, False])
def test_transport_failures_are_classified_without_retry(timeout: bool) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if timeout:
            raise httpx.ReadTimeout("private text", request=request)
        raise httpx.ConnectError("private text", request=request)

    async def scenario() -> list[ModelEvent]:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            port = LangChainModelPort("dummy-key", base_url="https://test", client=client)
            try:
                return await _collect(port.stream(_request()))
            finally:
                await port.aclose()

    events = asyncio.run(scenario())
    assert len(requests) == 1
    assert len(events) == 1 and isinstance(events[0], ModelFailed)
    assert events[0].category is (
        ModelErrorCategory.TIMEOUT if timeout else ModelErrorCategory.CONNECTION
    )
    assert "private text" not in repr(events)


def test_invalid_request_is_rejected_before_transport() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        pytest.fail("Invalid input must not be sent")

    async def scenario() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            port = LangChainModelPort("dummy-key", base_url="https://test", client=client)
            try:
                for request in (
                    replace(_request(), messages=(ModelMessage(MessageRole.TOOL, "result"),)),
                    replace(
                        _request(), provider_options={"deepseek": {"thinking": {"type": "enabled"}}}
                    ),
                ):
                    events = await _collect(port.stream(request))
                    assert len(events) == 1 and isinstance(events[0], ModelFailed)
                    assert events[0].category is ModelErrorCategory.INVALID_REQUEST
            finally:
                await port.aclose()

    asyncio.run(scenario())
