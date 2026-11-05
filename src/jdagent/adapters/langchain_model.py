"""LangChain's DeepSeek integration behind the project's model protocol."""

import json
from collections.abc import AsyncGenerator, AsyncIterator
from contextlib import aclosing
from dataclasses import dataclass
from typing import Any, cast

import httpx
import openai
from langchain_core.messages import (
    AIMessage,
    AIMessageChunk,
    BaseMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)
from langchain_deepseek import ChatDeepSeek
from langsmith import tracing_context  # pyright: ignore[reportUnknownVariableType]
from pydantic import SecretStr

from jdagent.domain.json import JsonObject, normalize_json, require_object
from jdagent.domain.model import (
    MessageRole,
    ModelCapabilities,
    ModelErrorCategory,
    ModelEvent,
    ModelFailed,
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
class _CallFragments:
    call_id: str = ""
    name: str = ""
    arguments: str = ""


def _messages(request: ModelRequest) -> list[BaseMessage]:
    messages: list[BaseMessage] = [SystemMessage(part.content) for part in request.system_parts]
    for message in request.messages:
        if message.role is MessageRole.USER:
            messages.append(HumanMessage(content=message.content, name=message.name))
        elif message.role is MessageRole.ASSISTANT:
            messages.append(
                AIMessage(
                    content=message.content,
                    name=message.name,
                    tool_calls=[
                        {"id": call.call_id, "name": call.name, "args": call.arguments}
                        for call in message.tool_calls
                    ],
                )
            )
        else:
            if message.tool_call_id is None:
                raise ValueError("Tool messages require tool_call_id")
            messages.append(
                ToolMessage(
                    content=message.content,
                    tool_call_id=message.tool_call_id,
                    name=message.name,
                )
            )
    return messages


def _provider_options(request: ModelRequest) -> JsonObject:
    options = require_object(request.provider_options.get("deepseek", {}), "deepseek")
    thinking = options.get("thinking")
    if thinking is not None and require_object(thinking, "thinking").get("type") != "disabled":
        raise ValueError("Reasoning history is not supported")
    return {
        key: value
        for key, value in options.items()
        if key in {"tool_choice", "top_p", "reasoning_effort", "response_format"}
    }


def _token_count(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError("Invalid token usage")
    return value


def _usage(chunk: AIMessageChunk, previous: Usage | None) -> Usage | None:
    metadata = chunk.usage_metadata
    if metadata is None:
        return previous
    values = tuple(
        _token_count(value)
        for value in (
            metadata["input_tokens"],
            metadata["output_tokens"],
            metadata.get("input_token_details", {}).get("cache_read", 0),
            metadata.get("output_token_details", {}).get("reasoning", 0),
        )
    )
    before = previous or Usage()
    # Provider chunks contain cumulative counts, not independently billable deltas.
    return Usage(
        max(before.input_tokens, values[0]),
        max(before.output_tokens, values[1]),
        max(before.cached_tokens, values[2]),
        max(before.reasoning_tokens, values[3]),
    )


def _http_category(status: int) -> ModelErrorCategory:
    if status == 401:
        return ModelErrorCategory.AUTHENTICATION
    if status == 403:
        return ModelErrorCategory.PERMISSION
    if status == 429:
        return ModelErrorCategory.RATE_LIMIT
    if status in (408, 504):
        return ModelErrorCategory.TIMEOUT
    if status in (400, 404, 409, 422):
        return ModelErrorCategory.INVALID_REQUEST
    if status >= 500:
        return ModelErrorCategory.PROVIDER_INTERNAL
    return ModelErrorCategory.INVALID_RESPONSE


class LangChainModelPort:
    """Translate validated SDK chunks; own only clients created by this adapter.

    The supplied HTTP client is borrowed. Each request gets its own model settings,
    while the underlying clients are reused. SDK retries and cloud tracing are disabled.
    """

    def __init__(
        self,
        api_key: str,
        *,
        base_url: str = "https://api.deepseek.com",
        client: httpx.AsyncClient | None = None,
    ) -> None:
        if not api_key:
            raise ValueError("DeepSeek API key is required")
        self._api_key = SecretStr(api_key)
        self._base_url = base_url
        self._owns_async_client = client is None
        self._async = openai.AsyncOpenAI(
            api_key=api_key, base_url=base_url, max_retries=0, http_client=client
        )
        # Supplying both SDK clients prevents ChatDeepSeek from making hidden clients.
        self._sync = openai.OpenAI(api_key=api_key, base_url=base_url, max_retries=0)
        self._closed = False

    @property
    def capabilities(self) -> ModelCapabilities:
        return ModelCapabilities(
            streaming=True, tool_calls=True, parallel_tool_calls=True, structured_output=True
        )

    async def aclose(self) -> None:
        """Close owned pools; borrowers remain responsible for their HTTP clients."""
        if self._closed:
            return
        self._closed = True
        try:
            if self._owns_async_client:
                await self._async.close()
        finally:
            self._sync.close()

    def _model(self, request: ModelRequest) -> ChatDeepSeek:
        return ChatDeepSeek(
            model=request.model,
            api_key=self._api_key,
            base_url=self._base_url,
            client=self._sync.chat.completions,
            root_client=self._sync,
            async_client=self._async.chat.completions,
            root_async_client=self._async,
            max_retries=0,
            temperature=request.settings.temperature,
            max_tokens=request.settings.max_output_tokens,
            timeout=request.settings.timeout_seconds,
            stream_usage=True,
            stream_chunk_timeout=None,
            extra_body={"thinking": {"type": "disabled"}},
        )

    async def stream(self, request: ModelRequest) -> AsyncIterator[ModelEvent]:
        """Stream transient deltas; publish calls and completion only after full validation."""
        if self._closed:
            yield ModelFailed(ModelErrorCategory.INVALID_REQUEST, "Model adapter is closed")
            return
        try:
            messages = _messages(request)
            options = _provider_options(request)
            model = self._model(request)
            # The SDK's dynamic keyword boundary is kept inside the adapter.
            kwargs: dict[str, Any] = {**options, "timeout": request.settings.timeout_seconds}
            if request.tools:
                model_call = model.bind_tools(  # pyright: ignore[reportUnknownMemberType]
                    [
                        {
                            "type": "function",
                            "function": {
                                "name": tool.name,
                                "description": tool.description,
                                "parameters": tool.input_schema,
                            },
                        }
                        for tool in request.tools
                    ],
                    tool_choice=cast(Any, options.get("tool_choice", "auto")),
                )
                kwargs.pop("tool_choice", None)
            else:
                model_call = model
        except (TypeError, ValueError):
            yield ModelFailed(ModelErrorCategory.INVALID_REQUEST, "Invalid model request")
            return

        calls: dict[int, _CallFragments] = {}
        usage: Usage | None = None
        finish: str | None = None
        failure: ModelFailed | None = None
        completed: list[ToolCall] = []
        try:
            # Do not inherit ambient LangSmith credentials or caller tracing callbacks.
            with tracing_context(enabled=False):
                # Locked LangChain implementations are async generators, despite the wider stub.
                chunks = cast(
                    AsyncGenerator[BaseMessage, None],
                    model_call.astream(messages, config={"callbacks": []}, **kwargs),
                )
                async with aclosing(chunks) as stream:
                    async for chunk in stream:
                        if not isinstance(chunk, AIMessageChunk) or not isinstance(
                            chunk.content, str
                        ):
                            raise ValueError("Unexpected model chunk")
                        usage = _usage(chunk, usage)
                        if chunk.content:
                            yield TextDelta(chunk.content)
                        for fragment in chunk.tool_call_chunks:
                            index = fragment.get("index")
                            if index is None or index < 0:
                                raise ValueError("Tool fragment has no valid index")
                            call = calls.setdefault(index, _CallFragments())
                            if fragment.get("id"):
                                call.call_id = fragment["id"] or ""
                            name, arguments = fragment.get("name") or "", fragment.get("args") or ""
                            call.name += name
                            call.arguments += arguments
                            yield ToolCallDelta(call.call_id, name, arguments)
                        reason = chunk.response_metadata.get("finish_reason")
                        if reason is not None:
                            if finish is not None or not isinstance(reason, str):
                                raise ValueError("Duplicate or invalid finish reason")
                            finish = reason
            if finish is None:
                raise ValueError("Missing finish reason")
            if (calls and finish not in {"tool_calls", "length"}) or (
                not calls and finish == "tool_calls"
            ):
                raise ValueError("Tool calls disagree with finish reason")
            seen: set[str] = set()
            for index in sorted(calls):
                call = calls[index]
                if not call.call_id or not call.name or call.call_id in seen:
                    raise ValueError("Missing or duplicate tool identity")
                seen.add(call.call_id)
                # LangChain accepts partial JSON while streaming. Execution requires strict JSON.
                arguments = require_object(
                    normalize_json(json.loads(call.arguments or "{}")), "arguments"
                )
                completed.append(ToolCall(call.call_id, call.name, arguments))
        except openai.APITimeoutError:
            failure = ModelFailed(ModelErrorCategory.TIMEOUT, "DeepSeek request timed out")
        except openai.APIConnectionError:
            failure = ModelFailed(ModelErrorCategory.CONNECTION, "DeepSeek connection failed")
        except openai.APIStatusError as error:
            failure = ModelFailed(
                _http_category(error.status_code),
                f"DeepSeek request failed with HTTP {error.status_code}",
                provider_code=str(error.status_code),
                request_id=error.request_id,
            )
        except (ValueError, TypeError, KeyError, openai.APIResponseValidationError):
            failure = ModelFailed(ModelErrorCategory.INVALID_RESPONSE, "Invalid DeepSeek response")
        if usage is not None:
            yield UsageReported(usage)
        if failure is not None:
            yield failure
            return
        for call in completed:
            yield ToolCallCompleted(call)
        assert finish is not None
        yield ResponseCompleted(finish)
