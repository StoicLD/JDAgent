import asyncio
from collections.abc import AsyncIterator
from pathlib import Path
from typing import TypeVar

import pytest

from jdagent.adapters.fake import FakeApproval, FakeModelPort
from jdagent.adapters.memory import InMemorySession
from jdagent.composition import RuntimeConfiguration
from jdagent.domain.events import (
    RuntimeEvent,
    RuntimeEventType,
    SessionStartedPayload,
    UserMessagePayload,
)
from jdagent.domain.model import (
    MessageRole,
    ModelCapabilities,
    ModelMessage,
    ModelRequest,
    ResponseCompleted,
    TextDelta,
)
from jdagent.domain.tools import ApprovalDecision, ApprovalRequest, RiskLevel

T = TypeVar("T")


async def _collect(items: AsyncIterator[T]) -> list[T]:
    return [item async for item in items]


def test_fake_model_replays_scripted_events() -> None:
    request = ModelRequest(model="fake", messages=(ModelMessage(MessageRole.USER, "hello"),))
    scripted = (TextDelta("hi"), ResponseCompleted("stop"))
    model = FakeModelPort(scripts=(scripted,), capabilities=ModelCapabilities())

    events = asyncio.run(_collect(model.stream(request)))

    assert events == list(scripted)
    assert model.requests == [request]


def test_in_memory_session_preserves_event_sequence() -> None:
    session = InMemorySession()
    first = RuntimeEvent.create(
        session_id="session-1",
        turn_id=None,
        sequence=1,
        event_type=RuntimeEventType.SESSION_STARTED,
        payload=SessionStartedPayload(),
    )
    second = RuntimeEvent.create(
        session_id="session-1",
        turn_id="turn-1",
        sequence=2,
        event_type=RuntimeEventType.USER_MESSAGE,
        payload=UserMessagePayload(content="hello"),
    )

    async def scenario() -> list[RuntimeEvent]:
        await session.append(first)
        await session.append(second)
        return await _collect(session.read("session-1"))

    assert asyncio.run(scenario()) == [first, second]


def test_fake_approval_records_request() -> None:
    request = ApprovalRequest(
        tool_name="write_text_file",
        arguments={"path": "note.txt", "content": "hello"},
        risk=RiskLevel.WRITE,
    )
    approval = FakeApproval(decision=ApprovalDecision.APPROVE)

    decision = asyncio.run(approval.request(request))

    assert decision is ApprovalDecision.APPROVE
    assert approval.requests == [request]


def test_runtime_configuration_rejects_invalid_call_limits(tmp_path: Path) -> None:
    for model_calls, tool_calls in ((0, 1), (1, 0)):
        with pytest.raises(ValueError):
            RuntimeConfiguration(
                provider="fake",
                model="fake",
                workspace=tmp_path,
                session_directory=tmp_path / "sessions",
                max_model_calls=model_calls,
                max_tool_calls=tool_calls,
            )
