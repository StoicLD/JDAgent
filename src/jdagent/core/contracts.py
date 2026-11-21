"""定义执行一轮对话所需的输入、依赖和结果。"""

from dataclasses import dataclass
from typing import Protocol

from jdagent.domain.errors import StopReason
from jdagent.domain.events import CitationRecord
from jdagent.domain.model import ModelEvent
from jdagent.domain.tools import ToolExecutionContext
from jdagent.knowledge.ptk import PreparedTurnKnowledge


class ModelEventObserver(Protocol):
    """接收显示等用途的临时模型流事件。会话的持久记录通过事件日志写入。"""

    async def observe(self, event: ModelEvent) -> None: ...


@dataclass(frozen=True, slots=True)
class LoopLimits:
    """规定一轮内允许的模型调用、工具调用和失败恢复次数。图的节点步数另行限制。"""

    max_model_calls: int = 16
    max_tool_calls: int = 32
    max_failure_recoveries: int = 2

    def __post_init__(self) -> None:
        if self.max_model_calls <= 0 or self.max_tool_calls <= 0:
            raise ValueError("Loop limits must be positive")


@dataclass(frozen=True, slots=True)
class TurnResult:
    """一轮执行的最终结果，包含停止原因、可展示的回答、调用次数及引用。"""

    stop_reason: StopReason
    assistant_text: str
    model_calls: int
    tool_calls: int
    error_category: str | None = None
    citations: tuple[CitationRecord, ...] = ()
    model_supplement: str = ""


class CancellationToken:
    """由请求方设置的取消信号，执行器在可以安全停止的位置检查它。"""

    def __init__(self) -> None:
        self._cancelled = False

    @property
    def cancelled(self) -> bool:
        return self._cancelled

    def cancel(self) -> None:
        self._cancelled = True


class TurnRunner(Protocol):
    """单轮执行接口。调用方提供轮次标识、工具上下文和可选的取消信号，得到最终结果。"""

    async def run(
        self,
        turn_id: str,
        tool_context: ToolExecutionContext,
        *,
        cancellation: CancellationToken | None = None,
        knowledge: PreparedTurnKnowledge | None = None,
    ) -> TurnResult: ...
