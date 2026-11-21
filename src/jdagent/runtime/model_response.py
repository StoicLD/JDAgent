"""在一次模型流结束时检查文本、工具参数和结束信号是否构成完整响应。"""

from jdagent.domain.errors import StopReason
from jdagent.domain.model import ResponseCompleted, TextDelta, ToolCallCompleted, ToolCallDelta
from jdagent.domain.tools import ToolCall


class ResponseProtocolError(Exception):
    """表示模型响应无法按协议处理，同时携带本轮应记录的错误类别和停止原因。"""

    def __init__(
        self,
        message: str,
        category: str = "invalid_response",
        reason: StopReason = StopReason.MODEL_ERROR,
    ) -> None:
        super().__init__(message)
        self.reason = reason
        self.category = category


class ModelResponse:
    """累积同一次请求返回的文本与工具调用，跟踪工具参数是否收齐以及模型给出的结束原因。"""

    def __init__(self, *, repair: bool) -> None:
        self.repair = repair
        self.text_parts: list[str] = []
        self.calls: list[ToolCall] = []
        self._finish: str | None = None
        self._unfinished_delta = False

    def accept(
        self, event: TextDelta | ToolCallDelta | ToolCallCompleted | ResponseCompleted
    ) -> None:
        """接收一条内容或结束事件。引用修复只允许文本，普通响应只允许一个结束信号。"""
        if isinstance(event, TextDelta):
            self.text_parts.append(event.text)
        elif isinstance(event, ResponseCompleted):
            if self._finish is not None and not self.repair:
                raise ResponseProtocolError("Model emitted more than one completion event")
            self._finish = event.finish_reason
        elif self.repair:
            raise ResponseProtocolError("Citation repair cannot request tools")
        elif isinstance(event, ToolCallDelta):
            self._unfinished_delta = True
        else:
            self._unfinished_delta = False
            self.calls.append(event.call)

    def validate(self) -> None:
        """在流关闭后核对结束原因和内容。模型声明调用工具时，至少要有一个已经收齐参数的调用。"""
        if (
            self._finish is None
            or self._unfinished_delta
            or (self.repair and self._finish != "stop")
        ):
            raise ResponseProtocolError("Model stream ended without a valid completion")
        if self._finish == "length":
            raise ResponseProtocolError(
                "Model response reached its output limit",
                "model_finish_length",
                StopReason.LIMIT_REACHED,
            )
        if self._finish == "tool_calls" and not self.calls:
            raise ResponseProtocolError("Model reported tool_calls without a complete tool call")
        if self.calls and self._finish != "tool_calls":
            raise ResponseProtocolError(
                "Model emitted tool calls with an incompatible finish reason"
            )
        if not self.calls and self._finish != "stop":
            raise ResponseProtocolError(
                "Model response did not finish normally",
                f"model_finish_{self._finish or 'unknown'}",
            )
