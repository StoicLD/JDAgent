import asyncio
from collections.abc import AsyncIterator
from pathlib import Path
from typing import TypeVar

import pytest

from jdagent.adapters.deepseek import DeepSeekModelPort
from jdagent.adapters.fake import FakeApproval, FakeModelPort
from jdagent.adapters.memory import InMemorySession
from jdagent.composition import RuntimeOptions, build_runtime, load_deepseek_api_key
from jdagent.configuration import ResolvedConfiguration
from jdagent.data_paths import DataPaths
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


def _resolved_configuration(
    tmp_path: Path,
    *,
    provider: str = "fake",
    api_key: str | None = None,
    api_key_file: Path | None = None,
) -> ResolvedConfiguration:
    paths = DataPaths.for_workspace(
        tmp_path,
        config_root=tmp_path / "config",
        data_root=tmp_path / "data",
    )
    return ResolvedConfiguration(
        provider=provider,
        model="deepseek-v4-flash" if provider == "deepseek" else "fake",
        base_url="https://api.deepseek.com",
        model_timeout_seconds=30.0,
        tool_timeout_seconds=10.0,
        max_context_tokens=None,
        fake_delay_seconds=0.0,
        write_permission="ask",
        api_key=api_key,
        api_key_file=api_key_file,
        workspace=tmp_path,
        session_directory=tmp_path / "sessions",
        data_paths=paths,
    )


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

    outcome = asyncio.run(approval.request(request))

    assert outcome.decision is ApprovalDecision.APPROVE
    assert outcome.granted_rule is None
    assert approval.requests == [request]


def test_runtime_configuration_rejects_invalid_call_limits(tmp_path: Path) -> None:
    for model_calls, tool_calls in ((0, 1), (1, 0)):
        with pytest.raises(ValueError):
            RuntimeOptions(
                max_model_calls=model_calls,
                max_tool_calls=tool_calls,
            )


def test_deepseek_key_uses_file_when_explicit_value_is_missing(tmp_path: Path) -> None:
    key_file = tmp_path / "deepseek-api-key.txt"
    key_file.write_text("file-development-key\n", encoding="utf-8")

    loaded = load_deepseek_api_key(None, key_file)

    assert loaded == "file-development-key"


def test_deepseek_key_prefers_explicit_value_without_reading_file(tmp_path: Path) -> None:
    missing_file = tmp_path / "missing-key.txt"

    loaded = load_deepseek_api_key(" explicit-development-key ", missing_file)

    assert loaded == "explicit-development-key"


def test_build_runtime_uses_development_key_file_when_explicit_key_is_missing(
    tmp_path: Path,
) -> None:
    key_file = tmp_path / "deepseek-api-key.txt"
    key_file.write_text("file-development-key\n", encoding="utf-8")
    configuration = _resolved_configuration(
        tmp_path,
        provider="deepseek",
        api_key_file=key_file,
    )

    composition = build_runtime(
        configuration,
        FakeApproval(decision=ApprovalDecision.APPROVE),
    )

    assert isinstance(composition.model, DeepSeekModelPort)
    asyncio.run(composition.aclose())


def test_resolved_configuration_repr_does_not_expose_explicit_key(tmp_path: Path) -> None:
    configuration = _resolved_configuration(
        tmp_path,
        provider="deepseek",
        api_key="repr-must-not-contain-this-key",
    )

    assert "repr-must-not-contain-this-key" not in repr(configuration)
