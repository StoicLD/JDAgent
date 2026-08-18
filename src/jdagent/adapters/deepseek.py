"""DeepSeek Chat Completions adapter implemented without provider SDK leakage."""

import json
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import cast

import httpx

from jdagent.domain.json import (
    JsonObject,
    JsonValue,
    normalize_json,
    optional_string,
    require_array,
    require_integer,
    require_object,
)
from jdagent.domain.model import (
    MessageRole,
    ModelCapabilities,
    ModelErrorCategory,
    ModelEvent,
    ModelFailed,
    ModelMessage,
    ModelRequest,
    ResponseCompleted,
    TextDelta,
    ToolCallCompleted,
    ToolCallDelta,
    Usage,
    UsageReported,
)
from jdagent.domain.tools import ToolCall


@dataclass(slots=True)
class _ToolFragments:
    call_id: str = ""
    name: str = ""
    arguments: str = ""


def _message_data(message: ModelMessage) -> JsonObject:
    data: JsonObject = {"role": message.role.value, "content": message.content}
    if message.role is MessageRole.ASSISTANT and message.tool_calls:
        data["tool_calls"] = [
            {
                "id": call.call_id,
                "type": "function",
                "function": {
                    "name": call.name,
                    "arguments": json.dumps(
                        call.arguments, ensure_ascii=False, separators=(",", ":")
                    ),
                },
            }
            for call in message.tool_calls
        ]
    if message.role is MessageRole.TOOL:
        if message.tool_call_id is None:
            raise ValueError("Tool messages require tool_call_id")
        data["tool_call_id"] = message.tool_call_id
    if message.name is not None:
        data["name"] = message.name
    return data


def _request_data(request: ModelRequest) -> JsonObject:
    messages: list[JsonValue] = [
        {"role": "system", "content": part.content} for part in request.system_parts
    ]
    messages.extend(_message_data(message) for message in request.messages)
    data: JsonObject = {
        "model": request.model,
        "messages": messages,
        "stream": True,
        "stream_options": {"include_usage": True},
        "thinking": {"type": "disabled"},
    }
    if request.tools:
        data["tools"] = [
            {
                "type": "function",
                "function": {
                    "name": tool.name,
                    "description": tool.description,
                    "parameters": tool.input_schema,
                },
            }
            for tool in request.tools
        ]
        data["tool_choice"] = "auto"
    if request.settings.temperature is not None:
        data["temperature"] = request.settings.temperature
    if request.settings.max_output_tokens is not None:
        data["max_tokens"] = request.settings.max_output_tokens

    provider_options = request.provider_options.get("deepseek")
    if provider_options is not None:
        options = require_object(provider_options, "provider_options.deepseek")
        allowed = {"tool_choice", "top_p", "reasoning_effort"}
        for name in allowed:
            if name in options:
                data[name] = options[name]
        thinking = options.get("thinking")
        if thinking is not None:
            thinking_data = require_object(thinking, "thinking")
            if thinking_data.get("type") != "disabled":
                raise ValueError(
                    "v0.1 does not enable DeepSeek thinking mode because reasoning history "
                    "is not projected"
                )
    return data


def _error_category(status_code: int) -> ModelErrorCategory:
    if status_code == 401:
        return ModelErrorCategory.AUTHENTICATION
    if status_code == 403:
        return ModelErrorCategory.PERMISSION
    if status_code == 429:
        return ModelErrorCategory.RATE_LIMIT
    if status_code in (408, 504):
        return ModelErrorCategory.TIMEOUT
    if status_code in (400, 404, 409, 422):
        return ModelErrorCategory.INVALID_REQUEST
    if status_code >= 500:
        return ModelErrorCategory.PROVIDER_INTERNAL
    return ModelErrorCategory.INVALID_RESPONSE


class DeepSeekModelPort:
    """Translate DeepSeek SSE chunks into the stable ModelPort event vocabulary."""

    def __init__(
        self,
        api_key: str,
        *,
        base_url: str = "https://api.deepseek.com",
        client: httpx.AsyncClient | None = None,
    ) -> None:
        if not api_key:
            raise ValueError("DeepSeek API key is required")
        self._api_key = api_key
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(base_url=base_url.rstrip("/"))
        self._capabilities = ModelCapabilities(
            streaming=True,
            tool_calls=True,
            parallel_tool_calls=True,
            structured_output=True,
            reasoning=False,
            context_window=None,
        )

    @property
    def capabilities(self) -> ModelCapabilities:
        return self._capabilities

    async def aclose(self) -> None:
        """Close the internally owned HTTP client."""

        if self._owns_client:
            await self._client.aclose()

    async def stream(self, request: ModelRequest) -> AsyncIterator[ModelEvent]:
        try:
            payload = _request_data(request)
        except (TypeError, ValueError) as error:
            yield ModelFailed(ModelErrorCategory.INVALID_REQUEST, str(error))
            return

        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Accept": "text/event-stream",
            "Content-Type": "application/json",
        }
        fragments: dict[int, _ToolFragments] = {}
        try:
            async with self._client.stream(
                "POST",
                "/chat/completions",
                headers=headers,
                json=payload,
                timeout=request.settings.timeout_seconds,
            ) as response:
                request_id = response.headers.get("x-request-id")
                if response.status_code < 200 or response.status_code >= 300:
                    yield ModelFailed(
                        _error_category(response.status_code),
                        f"DeepSeek request failed with HTTP {response.status_code}",
                        provider_code=str(response.status_code),
                        request_id=request_id,
                    )
                    return
                async for line in response.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    data_text = line[5:].strip()
                    if not data_text or data_text == "[DONE]":
                        continue
                    try:
                        loaded = cast(object, json.loads(data_text))
                        converted = normalize_json(loaded)
                        chunk = require_object(converted, "chunk")
                        for event in self._decode_chunk(chunk, fragments):
                            yield event
                    except (json.JSONDecodeError, TypeError, ValueError) as error:
                        yield ModelFailed(
                            ModelErrorCategory.INVALID_RESPONSE,
                            f"Invalid DeepSeek stream chunk: {type(error).__name__}",
                            request_id=request_id,
                        )
                        return
        except httpx.TimeoutException:
            yield ModelFailed(ModelErrorCategory.TIMEOUT, "DeepSeek request timed out")
        except httpx.RequestError:
            yield ModelFailed(ModelErrorCategory.CONNECTION, "DeepSeek connection failed")

    @staticmethod
    def _decode_chunk(
        chunk: JsonObject,
        fragments: dict[int, _ToolFragments],
    ) -> list[ModelEvent]:
        events: list[ModelEvent] = []
        completion_events: list[ModelEvent] = []

        choices_value = chunk.get("choices", [])
        for choice_value in require_array(choices_value, "choices"):
            choice = require_object(choice_value, "choice")
            delta = require_object(choice.get("delta", {}), "delta")
            content = optional_string(delta, "content")
            if content:
                events.append(TextDelta(content))
            tool_calls_value = delta.get("tool_calls", [])
            for tool_value in require_array(tool_calls_value, "tool_calls"):
                tool = require_object(tool_value, "tool_call")
                index = require_integer(tool, "index")
                current = fragments.setdefault(index, _ToolFragments())
                call_id = optional_string(tool, "id")
                function_value = tool.get("function")
                function = require_object(function_value, "function") if function_value else {}
                name = optional_string(function, "name") or ""
                arguments = optional_string(function, "arguments") or ""
                if call_id:
                    current.call_id = call_id
                current.name += name
                current.arguments += arguments
                events.append(ToolCallDelta(current.call_id, name, arguments))

            finish_reason = optional_string(choice, "finish_reason")
            if finish_reason is not None:
                for index in sorted(fragments):
                    current = fragments[index]
                    if not current.call_id or not current.name:
                        raise ValueError("Tool call is missing id or name")
                    loaded_arguments = cast(object, json.loads(current.arguments or "{}"))
                    arguments = normalize_json(loaded_arguments)
                    if not isinstance(arguments, dict):
                        raise ValueError("Tool arguments must be a JSON object")
                    completion_events.append(
                        ToolCallCompleted(ToolCall(current.call_id, current.name, arguments))
                    )
                fragments.clear()
                completion_events.append(ResponseCompleted(finish_reason))
        usage_value = chunk.get("usage")
        if usage_value is not None:
            usage = require_object(usage_value, "usage")
            details_value = usage.get("completion_tokens_details")
            details = (
                require_object(details_value, "completion_tokens_details") if details_value else {}
            )
            events.append(
                UsageReported(
                    Usage(
                        require_integer(usage, "prompt_tokens"),
                        require_integer(usage, "completion_tokens"),
                        cached_tokens=require_integer(usage, "prompt_cache_hit_tokens"),
                        reasoning_tokens=require_integer(details, "reasoning_tokens"),
                    )
                )
            )
        events.extend(completion_events)
        return events
