"""Framework-independent contracts shared by the native and graph turn runners."""

from dataclasses import dataclass
from typing import Protocol

from jdagent.domain.errors import StopReason
from jdagent.domain.events import CitationRecord
from jdagent.domain.model import ModelEvent
from jdagent.domain.tools import ToolExecutionContext
from jdagent.knowledge.ptk import PreparedTurnKnowledge


class ModelEventObserver(Protocol):
    """Consume transient model events such as displayable text deltas."""

    async def observe(self, event: ModelEvent) -> None: ...


@dataclass(frozen=True, slots=True)
class LoopLimits:
    """Hard call-count limits for one turn, independent of graph step counts."""

    max_model_calls: int = 16
    max_tool_calls: int = 32
    max_failure_recoveries: int = 2

    def __post_init__(self) -> None:
        if self.max_model_calls <= 0 or self.max_tool_calls <= 0:
            raise ValueError("Loop limits must be positive")


@dataclass(frozen=True, slots=True)
class TurnResult:
    """The caller-visible result of one complete turn."""

    stop_reason: StopReason
    assistant_text: str
    model_calls: int
    tool_calls: int
    error_category: str | None = None
    citations: tuple[CitationRecord, ...] = ()
    model_supplement: str = ""


class CancellationToken:
    """A cooperative cancellation signal checked at safe execution boundaries."""

    def __init__(self) -> None:
        self._cancelled = False

    @property
    def cancelled(self) -> bool:
        return self._cancelled

    def cancel(self) -> None:
        self._cancelled = True


class TurnRunner(Protocol):
    """Execute one turn through project services, without exposing framework state."""

    async def run(
        self,
        turn_id: str,
        tool_context: ToolExecutionContext,
        *,
        cancellation: CancellationToken | None = None,
        knowledge: PreparedTurnKnowledge | None = None,
    ) -> TurnResult: ...
