"""Deterministic terminal and presenter adapters for application tests."""

from collections.abc import Iterable

from jdagent.application.interactive import (
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

    async def next_action(self, state: InteractionState) -> UserAction:
        del state
        if not self._actions:
            return ExitAction()
        return self._actions.pop(0)

    async def close(self) -> None:
        self.closed = True

    async def update_status(self, status: TerminalStatus) -> None:
        self.statuses.append(status)


class CapturePresenter:
    """Capture semantic UI events without terminal rendering details."""

    def __init__(self) -> None:
        self.events: list[UiEvent] = []

    async def publish(self, event: UiEvent) -> None:
        self.events.append(event)
