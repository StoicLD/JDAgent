"""Collect one model response and validate its completion before dispatching tools."""

from jdagent.domain.errors import StopReason
from jdagent.domain.model import ResponseCompleted, TextDelta, ToolCallCompleted, ToolCallDelta
from jdagent.domain.tools import ToolCall


class ResponseProtocolError(Exception):
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
    """Accumulate content independently of display, accounting, and graph routing."""

    def __init__(self, *, repair: bool) -> None:
        self.repair = repair
        self.text_parts: list[str] = []
        self.calls: list[ToolCall] = []
        self._finish: str | None = None
        self._unfinished_delta = False

    def accept(
        self, event: TextDelta | ToolCallDelta | ToolCallCompleted | ResponseCompleted
    ) -> None:
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
