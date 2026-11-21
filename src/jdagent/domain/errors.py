"""定义对话停止原因以及可供上层识别的会话错误。"""

from enum import StrEnum


class StopReason(StrEnum):
    """描述整轮对话结束的原因，调用方据此区分正常完成、取消、预算耗尽和故障。"""

    COMPLETED = "completed"
    CANCELLED = "cancelled"
    MODEL_ERROR = "model_error"
    TOOL_ERROR = "tool_error"
    CONTEXT_LIMIT = "context_limit"
    LIMIT_REACHED = "limit_reached"
    SESSION_ERROR = "session_error"
    INTERNAL_ERROR = "internal_error"


class SessionErrorCode(StrEnum):
    """会话存储与恢复错误的稳定分类，供上层决定如何处理。"""

    NOT_FOUND = "not_found"
    CORRUPT_EVENT = "corrupt_event"
    UNSUPPORTED_SCHEMA = "unsupported_schema"
    APPEND_FAILED = "append_failed"
    READ_FAILED = "read_failed"
    BUSY = "session_busy"


class SessionError(RuntimeError):
    """携带错误分类的会话异常，用于报告读取、追加或取得会话所有权失败。"""

    def __init__(self, code: SessionErrorCode, message: str) -> None:
        super().__init__(message)
        self.code = code
