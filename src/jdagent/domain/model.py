"""Provider-independent model request and streaming event types."""

from dataclasses import dataclass, field
from enum import StrEnum
from typing import TypeAlias

from jdagent.domain.json import JsonObject
from jdagent.domain.tools import ToolCall


def _empty_string_map() -> dict[str, str]:
    return {}


def _empty_json_object() -> JsonObject:
    return {}


class MessageRole(StrEnum):
    """Roles understood by the runtime message projection."""

    USER = "user"
    ASSISTANT = "assistant"
    TOOL = "tool"


class ModelErrorCategory(StrEnum):
    """Stable failures at the model seam."""

    AUTHENTICATION = "authentication"
    PERMISSION = "permission"
    RATE_LIMIT = "rate_limit"
    TIMEOUT = "timeout"
    CONNECTION = "connection"
    CONTEXT_LENGTH = "context_length"
    INVALID_REQUEST = "invalid_request"
    INVALID_RESPONSE = "invalid_response"
    PROVIDER_INTERNAL = "provider_internal"
    CANCELLED = "cancelled"


@dataclass(frozen=True, slots=True)
class ModelCapabilities:
    """Capabilities declared by a concrete model adapter."""

    streaming: bool = True
    tool_calls: bool = True
    parallel_tool_calls: bool = False
    structured_output: bool = False
    reasoning: bool = False
    context_window: int | None = None


@dataclass(frozen=True, slots=True)
class SystemPart:
    """A system instruction with an inspectable source."""

    content: str
    source: str = "runtime"


@dataclass(frozen=True, slots=True)
class ModelMessage:
    """A provider-independent projected conversation message."""

    role: MessageRole
    content: str
    tool_calls: tuple[ToolCall, ...] = ()
    tool_call_id: str | None = None
    name: str | None = None


@dataclass(frozen=True, slots=True)
class ModelToolDefinition:
    """The model-visible portion of a registered tool."""

    name: str
    description: str
    input_schema: JsonObject


@dataclass(frozen=True, slots=True)
class ModelSettings:
    """Provider-independent settings approved for v0.1."""

    temperature: float | None = None
    max_output_tokens: int | None = None
    timeout_seconds: float = 30.0


@dataclass(frozen=True, slots=True)
class ModelRequest:
    """A complete request consumed by ModelPort."""

    model: str
    messages: tuple[ModelMessage, ...]
    system_parts: tuple[SystemPart, ...] = ()
    tools: tuple[ModelToolDefinition, ...] = ()
    settings: ModelSettings = field(default_factory=ModelSettings)
    metadata: dict[str, str] = field(default_factory=_empty_string_map)
    provider_options: JsonObject = field(default_factory=_empty_json_object)


@dataclass(frozen=True, slots=True)
class Usage:
    """Token usage reported by a provider when available."""

    input_tokens: int = 0
    output_tokens: int = 0
    cached_tokens: int = 0
    reasoning_tokens: int = 0


@dataclass(frozen=True, slots=True)
class TextDelta:
    """A displayable text fragment."""

    text: str


@dataclass(frozen=True, slots=True)
class ToolCallDelta:
    """An incomplete tool-call fragment that cannot be executed."""

    call_id: str
    name_fragment: str = ""
    arguments_fragment: str = ""


@dataclass(frozen=True, slots=True)
class ToolCallCompleted:
    """A complete tool call eligible for ToolRuntimePort."""

    call: ToolCall


@dataclass(frozen=True, slots=True)
class UsageReported:
    """Usage emitted during or after a model response."""

    usage: Usage


@dataclass(frozen=True, slots=True)
class ResponseCompleted:
    """The provider-level reason a model response ended."""

    finish_reason: str


@dataclass(frozen=True, slots=True)
class ModelFailed:
    """A safely classified model failure."""

    category: ModelErrorCategory
    message: str
    provider_code: str | None = None
    request_id: str | None = None


ModelEvent: TypeAlias = (
    TextDelta | ToolCallDelta | ToolCallCompleted | UsageReported | ResponseCompleted | ModelFailed
)
