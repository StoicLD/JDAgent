"""Deterministic adapters for tests and offline demonstrations."""

import asyncio
from collections.abc import AsyncIterator

from jdagent.domain.model import ModelCapabilities, ModelEvent, ModelRequest
from jdagent.domain.tools import (
    ApprovalDecision,
    ApprovalRequest,
    ToolCall,
    ToolExecutionContext,
    ToolResult,
)


class FakeModelPort:
    """Replays one configured event script per model call."""

    def __init__(
        self,
        *,
        scripts: tuple[tuple[ModelEvent, ...], ...],
        capabilities: ModelCapabilities | None = None,
        repeat_last: bool = False,
        delay_seconds: float = 0.0,
    ) -> None:
        if not scripts:
            raise ValueError("FakeModelPort requires at least one script")
        if delay_seconds < 0:
            raise ValueError("FakeModelPort delay_seconds cannot be negative")
        self._scripts = scripts
        self._capabilities = capabilities or ModelCapabilities()
        self._repeat_last = repeat_last
        self._delay_seconds = delay_seconds
        self._next_script = 0
        self.requests: list[ModelRequest] = []

    @property
    def capabilities(self) -> ModelCapabilities:
        return self._capabilities

    async def stream(self, request: ModelRequest) -> AsyncIterator[ModelEvent]:
        self.requests.append(request)
        if self._delay_seconds:
            await asyncio.sleep(self._delay_seconds)
        if self._next_script >= len(self._scripts):
            if not self._repeat_last:
                raise RuntimeError("FakeModelPort has no remaining event script")
            script = self._scripts[-1]
        else:
            script = self._scripts[self._next_script]
            self._next_script += 1
        for event in script:
            yield event


class FakeApproval:
    """Returns one decision and records every approval request."""

    def __init__(self, decision: ApprovalDecision) -> None:
        self._decision = decision
        self.requests: list[ApprovalRequest] = []

    async def request(self, request: ApprovalRequest) -> ApprovalDecision:
        self.requests.append(request)
        return self._decision


class FakeToolRuntime:
    """Returns configured results without implementing tool behavior."""

    def __init__(self, results: tuple[ToolResult, ...]) -> None:
        self._results = list(results)
        self.calls: list[tuple[ToolCall, ToolExecutionContext]] = []

    async def execute(self, call: ToolCall, context: ToolExecutionContext) -> ToolResult:
        self.calls.append((call, context))
        if not self._results:
            raise RuntimeError("FakeToolRuntime has no remaining result")
        return self._results.pop(0)
