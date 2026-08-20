"""One-shot application seam shared by text and JSON presenters."""

from dataclasses import dataclass
from enum import IntEnum

from jdagent.application.coordinator import TurnCoordinator
from jdagent.domain.errors import StopReason
from jdagent.domain.json import JsonObject
from jdagent.observability import TraceProjection


class ExitStatus(IntEnum):
    """Stable process exit statuses for CLI callers."""

    SUCCESS = 0
    INTERNAL_ERROR = 1
    USAGE_OR_CONFIG_ERROR = 2
    SESSION_ERROR = 3
    RUNTIME_ERROR = 4
    CANCELLED = 130


@dataclass(frozen=True, slots=True)
class HeadlessResult:
    """Presenter-independent result of exactly one prompt."""

    session_id: str
    turn_id: str
    stop_reason: StopReason
    answer: str
    provider: str
    model: str
    input_tokens: int
    output_tokens: int
    error_category: str | None
    trace: TraceProjection

    @property
    def exit_status(self) -> ExitStatus:
        if self.stop_reason is StopReason.COMPLETED:
            return ExitStatus.SUCCESS
        if self.stop_reason is StopReason.CANCELLED:
            return ExitStatus.CANCELLED
        if self.stop_reason is StopReason.INTERNAL_ERROR:
            return ExitStatus.INTERNAL_ERROR
        return ExitStatus.RUNTIME_ERROR

    def json_data(self) -> JsonObject:
        """Return the stable JSON-v1 public representation."""

        error: JsonObject | None = None
        if self.exit_status is not ExitStatus.SUCCESS:
            error = {
                "category": self.error_category or self.stop_reason.value,
                "message": "Turn did not complete successfully",
            }
        return {
            "schema_version": 1,
            "status": "success" if self.exit_status is ExitStatus.SUCCESS else "error",
            "session_id": self.session_id,
            "turn_id": self.turn_id,
            "stop_reason": self.stop_reason.value,
            "answer": self.answer,
            "provider": self.provider,
            "model": self.model,
            "usage": {
                "input_tokens": self.input_tokens,
                "output_tokens": self.output_tokens,
                "total_tokens": self.input_tokens + self.output_tokens,
            },
            "error": error,
        }


async def run_headless(
    coordinator: TurnCoordinator,
    prompt: str,
    *,
    provider: str,
    model: str,
    session_id: str | None = None,
) -> HeadlessResult:
    """Run one prompt without binding the application to terminal output."""

    turn = await coordinator.send(prompt, session_id=session_id)
    summary = turn.trace.summary
    return HeadlessResult(
        session_id=turn.session_id,
        turn_id=turn.turn_id,
        stop_reason=turn.result.stop_reason,
        answer=turn.result.assistant_text,
        provider=summary.provider or provider,
        model=summary.model or model,
        input_tokens=summary.input_tokens,
        output_tokens=summary.output_tokens,
        error_category=turn.result.error_category,
        trace=turn.trace,
    )
