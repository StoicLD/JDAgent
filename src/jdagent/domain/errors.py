"""Runtime-level stop and session error types."""

from enum import StrEnum


class StopReason(StrEnum):
    """Stable reason why a complete agent turn stopped."""

    COMPLETED = "completed"
    CANCELLED = "cancelled"
    MODEL_ERROR = "model_error"
    CONTEXT_LIMIT = "context_limit"
    LIMIT_REACHED = "limit_reached"
    SESSION_ERROR = "session_error"
    INTERNAL_ERROR = "internal_error"


class SessionErrorCode(StrEnum):
    """Stable failures at the session seam."""

    NOT_FOUND = "not_found"
    CORRUPT_EVENT = "corrupt_event"
    UNSUPPORTED_SCHEMA = "unsupported_schema"
    APPEND_FAILED = "append_failed"
    READ_FAILED = "read_failed"


class SessionError(RuntimeError):
    """A classified session adapter failure."""

    def __init__(self, code: SessionErrorCode, message: str) -> None:
        super().__init__(message)
        self.code = code
