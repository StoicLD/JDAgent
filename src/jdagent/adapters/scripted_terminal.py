"""Deterministic terminal and presenter adapters for application tests."""

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
    """Return prearranged user actions through the production terminal seam."""

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
    """Capture semantic UI events without terminal rendering details."""

    def __init__(self) -> None:
        self.events: list[UiEvent] = []

    async def publish(self, event: UiEvent) -> None:
        self.events.append(event)
