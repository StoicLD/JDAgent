"""定义模型请求、消息及流式响应事件的公共格式。"""

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
    """区分投影到模型会话中的消息角色，使工具结果与用户、助手文本正确对应。"""

    USER = "user"
    ASSISTANT = "assistant"
    TOOL = "tool"


class ModelErrorCategory(StrEnum):
    """将不同服务商的模型错误归入稳定类别，供执行器决定如何结束或恢复。"""

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
    """描述具体模型适配器声明支持的能力，供构造请求前校验。"""

    streaming: bool = True
    tool_calls: bool = True
    parallel_tool_calls: bool = False
    structured_output: bool = False
    reasoning: bool = False
    context_window: int | None = None


@dataclass(frozen=True, slots=True)
class SystemPart:
    """记录一段系统级上下文及其来源，使注入请求的规则和材料可以追溯。"""

    content: str
    source: str = "runtime"


@dataclass(frozen=True, slots=True)
class ModelMessage:
    """表示投影给模型的一条消息，包括角色、文本以及适用时的工具调用关联。"""

    role: MessageRole
    content: str
    tool_calls: tuple[ToolCall, ...] = ()
    tool_call_id: str | None = None
    name: str | None = None


@dataclass(frozen=True, slots=True)
class ModelToolDefinition:
    """描述模型可见的工具名称、用途和参数结构；执行权限另由宿主判定。"""

    name: str
    description: str
    input_schema: JsonObject


@dataclass(frozen=True, slots=True)
class ModelSettings:
    """保存适用于一次请求的模型参数，例如输出上限与超时。"""

    temperature: float | None = None
    max_output_tokens: int | None = None
    timeout_seconds: float = 30.0


@dataclass(frozen=True, slots=True)
class ModelRequest:
    """模型适配器接收的完整输入，汇集消息、系统材料、可用工具、模型设置与请求元数据。"""

    model: str
    messages: tuple[ModelMessage, ...]
    system_parts: tuple[SystemPart, ...] = ()
    tools: tuple[ModelToolDefinition, ...] = ()
    settings: ModelSettings = field(default_factory=ModelSettings)
    metadata: dict[str, str] = field(default_factory=_empty_string_map)
    provider_options: JsonObject = field(default_factory=_empty_json_object)


@dataclass(frozen=True, slots=True)
class Usage:
    """记录服务商实际报告的 Token 用量，供归档和计量使用。"""

    input_tokens: int = 0
    output_tokens: int = 0
    cached_tokens: int = 0
    reasoning_tokens: int = 0


@dataclass(frozen=True, slots=True)
class TextDelta:
    """本次流式响应新增的一段文本，可以在整条响应完成前展示。"""

    text: str


@dataclass(frozen=True, slots=True)
class ToolCallDelta:
    """模型分片返回的工具参数或名称。参数未收齐时无法据此执行工具。"""

    call_id: str
    name_fragment: str = ""
    arguments_fragment: str = ""


@dataclass(frozen=True, slots=True)
class ToolCallCompleted:
    """表示一次调用的名称和参数已收齐，待整条响应通过校验后交给工具运行时。"""

    call: ToolCall


@dataclass(frozen=True, slots=True)
class UsageReported:
    """模型服务发出的用量事件，可能在响应结束信号之后到达。"""

    usage: Usage


@dataclass(frozen=True, slots=True)
class ResponseCompleted:
    """记录服务商给出的响应结束原因，执行器据此核对文本和工具调用是否完整。"""

    finish_reason: str


@dataclass(frozen=True, slots=True)
class ModelFailed:
    """保存经过分类和处理的模型错误，供执行器记录本轮失败原因。"""

    category: ModelErrorCategory
    message: str
    provider_code: str | None = None
    request_id: str | None = None


ModelEvent: TypeAlias = (
    TextDelta | ToolCallDelta | ToolCallCompleted | UsageReported | ResponseCompleted | ModelFailed
)
