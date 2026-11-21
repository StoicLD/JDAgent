"""用预设输入和可检查的输出替代交互终端，供应用测试使用。"""

import asyncio
from collections.abc import Iterable

from jdagent.application.interactive import (
    CommandAction,
    CommandName,
    ExitAction,
    InteractionState,
    TerminalStatus,
    UiEvent,
    UserAction,
)


class ScriptedTerminal:
    """按预设顺序向交互应用提供用户操作，使测试可走与真实终端相同的接口。"""

    def __init__(self, actions: Iterable[UserAction]) -> None:
        self._actions = list(actions)
        self.closed = False
        self.statuses: list[TerminalStatus] = []
        self._idle = asyncio.Event()

    async def next_action(self, state: InteractionState) -> UserAction:
        if state is not InteractionState.IDLE:
            following = self._actions[0] if self._actions else None
            if not isinstance(following, CommandAction) or following.name not in {
                CommandName.COMPACT,
                CommandName.CONTEXT_WINDOW,
            }:
                await self._idle.wait()
        if not self._actions:
            return ExitAction()
        return self._actions.pop(0)

    async def close(self) -> None:
        self.closed = True

    async def update_status(self, status: TerminalStatus) -> None:
        self.statuses.append(status)
        if status.state is InteractionState.IDLE:
            self._idle.set()
        else:
            self._idle.clear()


class CapturePresenter:
    """记录应用发出的界面事件，测试可据此核对提示与状态变化。"""

    def __init__(self) -> None:
        self.events: list[UiEvent] = []

    async def publish(self, event: UiEvent) -> None:
        self.events.append(event)
