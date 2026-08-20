import asyncio
import subprocess
import sys
from pathlib import Path

import pytest

from jdagent.adapters.jsonl_recovery import JsonlRecoveryStore
from jdagent.adapters.jsonl_session import JsonlSession
from jdagent.application.session_recovery import (
    LogicalRecovery,
    SessionRecovery,
    close_interrupted_turn,
)
from jdagent.domain.events import RuntimeEventType, TurnFailedPayload

_FIXTURE = Path(__file__).parent / "fixtures" / "forced_exit_writer.py"


def _force_exit(directory: Path, mode: str) -> None:
    completed = subprocess.run(
        [sys.executable, str(_FIXTURE), str(directory), mode],
        text=True,
        capture_output=True,
        check=False,
    )
    assert completed.returncode == 23
    assert completed.stdout == ""
    assert completed.stderr == ""


@pytest.mark.parametrize(
    ("mode", "expected"),
    (
        ("user-only", LogicalRecovery.INTERRUPTED_SAFE),
        ("write-started", LogicalRecovery.UNCERTAIN_SIDE_EFFECT),
    ),
)
def test_forced_exit_is_classified_from_durable_event_prefix(
    tmp_path: Path,
    mode: str,
    expected: LogicalRecovery,
) -> None:
    directory = tmp_path / "sessions"
    _force_exit(directory, mode)

    result = asyncio.run(SessionRecovery(JsonlRecoveryStore(directory)).recover("forced-session"))

    assert result.logical is expected


def test_forced_exit_before_side_effect_can_be_closed_and_resumed(tmp_path: Path) -> None:
    directory = tmp_path / "sessions"
    _force_exit(directory, "user-only")

    async def scenario() -> tuple[RuntimeEventType, str]:
        session = JsonlSession(directory)
        result = await SessionRecovery(JsonlRecoveryStore(directory)).recover("forced-session")
        await close_interrupted_turn(session=session, events=result.events)
        events = tuple([event async for event in session.read("forced-session")])
        payload = events[-1].payload
        assert isinstance(payload, TurnFailedPayload)
        return events[-1].event_type, payload.error_category

    event_type, category = asyncio.run(scenario())

    assert event_type is RuntimeEventType.TURN_FAILED
    assert category == "process_interrupted"
