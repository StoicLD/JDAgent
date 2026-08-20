"""Deterministic projection of session facts into model requests."""

import json
from collections.abc import Iterable, Sequence

from jdagent.domain.events import (
    AssistantMessageCompletedPayload,
    RecoverySnapshotPayload,
    RuntimeEvent,
    RuntimeEventType,
    ToolExecutionCompletedPayload,
    UserMessagePayload,
)
from jdagent.domain.json import JsonObject
from jdagent.domain.model import (
    MessageRole,
    ModelCapabilities,
    ModelMessage,
    ModelRequest,
    ModelSettings,
    ModelToolDefinition,
    SystemPart,
)
from jdagent.domain.tools import ToolDefinition, ToolResultStatus


class ContextLimitError(RuntimeError):
    """The complete projected context exceeds a known hard limit."""

    def __init__(self, estimated_tokens: int, limit_tokens: int) -> None:
        super().__init__(
            f"Estimated context size {estimated_tokens} exceeds hard limit {limit_tokens}"
        )
        self.estimated_tokens = estimated_tokens
        self.limit_tokens = limit_tokens


class UnsupportedModelCapabilityError(RuntimeError):
    """A request requires a capability the selected adapter does not declare."""


def project_messages(events: Iterable[RuntimeEvent]) -> tuple[ModelMessage, ...]:
    """Project durable conversation facts into provider-independent messages."""

    messages: list[ModelMessage] = []
    for event in events:
        if event.event_type is RuntimeEventType.RECOVERY_SNAPSHOT:
            payload = event.payload
            if not isinstance(payload, RecoverySnapshotPayload):
                raise TypeError("recovery_snapshot event has the wrong payload")
            messages.extend(payload.messages)
        elif event.event_type is RuntimeEventType.USER_MESSAGE:
            payload = event.payload
            if not isinstance(payload, UserMessagePayload):
                raise TypeError("user_message event has the wrong payload")
            messages.append(ModelMessage(MessageRole.USER, payload.content))
        elif event.event_type is RuntimeEventType.ASSISTANT_MESSAGE_COMPLETED:
            payload = event.payload
            if not isinstance(payload, AssistantMessageCompletedPayload):
                raise TypeError("assistant_message_completed event has the wrong payload")
            messages.append(
                ModelMessage(
                    MessageRole.ASSISTANT,
                    payload.content,
                    tool_calls=payload.tool_calls,
                )
            )
        elif event.event_type is RuntimeEventType.TOOL_EXECUTION_COMPLETED:
            payload = event.payload
            if not isinstance(payload, ToolExecutionCompletedPayload):
                raise TypeError("tool_execution_completed event has the wrong payload")
            result = payload.result
            if result.status is ToolResultStatus.SUCCESS:
                content = result.output
            else:
                error_code = result.error_code.value if result.error_code else "execution_failed"
                error_message = result.error_message or "Tool execution failed"
                content = f"Error [{error_code}]: {error_message}"
            messages.append(
                ModelMessage(
                    MessageRole.TOOL,
                    content,
                    tool_call_id=result.call_id,
                    name=result.tool_name,
                )
            )
    return tuple(messages)


class ContextBuilder:
    """Build complete ModelRequest values without dropping session history."""

    def __init__(
        self,
        *,
        model: str,
        system_parts: tuple[SystemPart, ...] = (),
        tools: tuple[ToolDefinition, ...] = (),
        settings: ModelSettings | None = None,
        max_context_tokens: int | None = None,
        provider_options: JsonObject | None = None,
    ) -> None:
        if max_context_tokens is not None and max_context_tokens <= 0:
            raise ValueError("max_context_tokens must be positive")
        self._model = model
        self._system_parts = system_parts
        self._tools = tools
        self._settings = settings or ModelSettings()
        self._max_context_tokens = max_context_tokens
        self._provider_options = provider_options or {}

    def build(
        self,
        events: Sequence[RuntimeEvent],
        capabilities: ModelCapabilities,
    ) -> ModelRequest:
        """Build and validate one deterministic provider-independent request."""

        if self._tools and not capabilities.tool_calls:
            raise UnsupportedModelCapabilityError("Selected model does not support tool calls")

        messages = project_messages(events)
        tools = tuple(
            ModelToolDefinition(tool.name, tool.description, dict(tool.input_schema))
            for tool in self._tools
        )
        metadata: dict[str, str] = {}
        if events:
            metadata["session_id"] = events[-1].session_id
            if events[-1].turn_id is not None:
                metadata["turn_id"] = events[-1].turn_id
        request = ModelRequest(
            model=self._model,
            system_parts=self._system_parts,
            messages=messages,
            tools=tools,
            settings=self._settings,
            metadata=metadata,
            provider_options=dict(self._provider_options),
        )

        estimated_tokens = self._estimate_tokens(request)
        limits = [
            limit
            for limit in (self._max_context_tokens, capabilities.context_window)
            if limit is not None
        ]
        if limits:
            hard_limit = min(limits)
            if estimated_tokens > hard_limit:
                raise ContextLimitError(estimated_tokens, hard_limit)
        return request

    @staticmethod
    def _estimate_tokens(request: ModelRequest) -> int:
        serialized_tools = json.dumps(
            [tool.input_schema for tool in request.tools],
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        text = "".join(part.content for part in request.system_parts)
        for message in request.messages:
            text += message.content
            for call in message.tool_calls:
                text += call.call_id + call.name
                text += json.dumps(
                    call.arguments,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
        text += serialized_tools
        return max(1, (len(text) + 3) // 4)
