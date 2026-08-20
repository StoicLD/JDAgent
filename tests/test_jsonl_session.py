import asyncio
from pathlib import Path

import pytest

from jdagent.adapters.fake import FakeApproval, FakeModelPort
from jdagent.adapters.jsonl_session import JsonlSession
from jdagent.domain.errors import SessionError, SessionErrorCode
from jdagent.domain.events import (
    PermissionRuleGrantedPayload,
    PermissionRuleRevokedPayload,
    RecoverySnapshotPayload,
    RuntimeEventType,
    SessionStartedPayload,
)
from jdagent.domain.model import (
    MessageRole,
    ModelMessage,
    ResponseCompleted,
    TextDelta,
    ToolCallCompleted,
)
from jdagent.domain.tools import (
    ApprovalDecision,
    PermissionTargetKind,
    SessionPermissionRule,
    ToolCall,
)
from jdagent.eventing import EventJournal
from jdagent.tools.builtins import create_builtin_tools
from jdagent.tools.workspace import WorkspacePathResolver
from tests.runtime_factory import create_test_coordinator


def test_jsonl_round_trip_preserves_events(tmp_path: Path) -> None:
    async def scenario() -> tuple[str, str]:
        session = JsonlSession(tmp_path / "sessions")
        journal = await EventJournal.open(session, "session-1")
        original = await journal.record(
            None,
            RuntimeEventType.SESSION_STARTED,
            SessionStartedPayload(),
        )
        reopened = JsonlSession(tmp_path / "sessions")
        events = [event async for event in reopened.read("session-1")]
        return original.event_id, events[0].event_id

    original_id, restored_id = asyncio.run(scenario())

    assert restored_id == original_id


def test_jsonl_round_trip_preserves_recovery_and_permission_facts(tmp_path: Path) -> None:
    async def scenario() -> tuple[object, object, object]:
        session = JsonlSession(tmp_path / "sessions")
        journal = await EventJournal.open(session, "session-1")
        await journal.record(
            None,
            RuntimeEventType.SESSION_STARTED,
            SessionStartedPayload("recovery", "workspace-1"),
        )
        snapshot = RecoverySnapshotPayload(
            "parent",
            7,
            (ModelMessage(MessageRole.USER, "safe context"),),
        )
        rule = SessionPermissionRule(
            "rule-1",
            "session-1",
            "write_text_file",
            PermissionTargetKind.FILE,
            "note.txt",
        )
        await journal.record(None, RuntimeEventType.RECOVERY_SNAPSHOT, snapshot)
        await journal.record(
            None,
            RuntimeEventType.PERMISSION_RULE_GRANTED,
            PermissionRuleGrantedPayload(rule),
        )
        await journal.record(
            None,
            RuntimeEventType.PERMISSION_RULE_REVOKED,
            PermissionRuleRevokedPayload("rule-1"),
        )

        restored = tuple([event async for event in session.read("session-1")])
        return restored[1].payload, restored[2].payload, restored[3].payload

    restored_snapshot, restored_grant, restored_revoke = asyncio.run(scenario())

    assert restored_snapshot == RecoverySnapshotPayload(
        "parent",
        7,
        (ModelMessage(MessageRole.USER, "safe context"),),
    )
    assert isinstance(restored_grant, PermissionRuleGrantedPayload)
    assert restored_grant.rule.target == "note.txt"
    assert restored_revoke == PermissionRuleRevokedPayload("rule-1")


def test_jsonl_reader_rejects_partial_final_line(tmp_path: Path) -> None:
    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    (session_dir / "session-1.jsonl").write_bytes(b'{"schema_version":1')
    session = JsonlSession(session_dir)

    async def read_all() -> None:
        _ = [event async for event in session.read("session-1")]

    with pytest.raises(SessionError) as captured:
        asyncio.run(read_all())

    assert captured.value.code is SessionErrorCode.CORRUPT_EVENT
    assert "byte offset 0" in str(captured.value)


def test_jsonl_reader_rejects_existing_empty_file(tmp_path: Path) -> None:
    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    (session_dir / "session-1.jsonl").write_bytes(b"")
    session = JsonlSession(session_dir)

    async def read_all() -> None:
        _ = [event async for event in session.read("session-1")]

    with pytest.raises(SessionError) as captured:
        asyncio.run(read_all())

    assert captured.value.code is SessionErrorCode.CORRUPT_EVENT


def test_resume_unknown_session_fails_instead_of_creating(tmp_path: Path) -> None:
    coordinator = create_test_coordinator(
        model=FakeModelPort(scripts=((TextDelta("unexpected"), ResponseCompleted("stop")),)),
        session=JsonlSession(tmp_path / "sessions"),
        tools=(),
        approval=FakeApproval(ApprovalDecision.APPROVE),
        workspace=tmp_path,
    )

    with pytest.raises(SessionError) as captured:
        asyncio.run(coordinator.send("hello", session_id="missing-session"))

    assert captured.value.code is SessionErrorCode.NOT_FOUND
    assert not (tmp_path / "sessions" / "missing-session.jsonl").exists()


def test_resume_rebuilds_context_from_events(tmp_path: Path) -> None:
    async def scenario() -> tuple[str, list[str]]:
        session = JsonlSession(tmp_path / "sessions")
        first_model = FakeModelPort(
            scripts=((TextDelta("first answer"), ResponseCompleted("stop")),)
        )
        first = create_test_coordinator(
            model=first_model,
            session=session,
            tools=(),
            approval=FakeApproval(ApprovalDecision.APPROVE),
            workspace=tmp_path,
        )
        first_turn = await first.send("first question")

        second_model = FakeModelPort(
            scripts=((TextDelta("second answer"), ResponseCompleted("stop")),)
        )
        resumed = create_test_coordinator(
            model=second_model,
            session=JsonlSession(tmp_path / "sessions"),
            tools=(),
            approval=FakeApproval(ApprovalDecision.APPROVE),
            workspace=tmp_path,
        )
        await resumed.send("second question", session_id=first_turn.session_id)
        return first_turn.session_id, [
            message.content for message in second_model.requests[0].messages
        ]

    session_id, messages = asyncio.run(scenario())

    assert session_id
    assert messages == ["first question", "first answer", "second question"]


def test_resume_preserves_permission_tool_and_stop_events(tmp_path: Path) -> None:
    async def scenario() -> set[RuntimeEventType]:
        session_dir = tmp_path / "sessions"
        call = ToolCall(
            "call-1",
            "write_text_file",
            {"path": "note.txt", "content": "blocked"},
        )
        model = FakeModelPort(
            scripts=(
                (ToolCallCompleted(call), ResponseCompleted("tool_calls")),
                (TextDelta("rejected"), ResponseCompleted("stop")),
            )
        )
        coordinator = create_test_coordinator(
            model=model,
            session=JsonlSession(session_dir),
            tools=create_builtin_tools(WorkspacePathResolver(tmp_path)),
            approval=FakeApproval(ApprovalDecision.REJECT),
            workspace=tmp_path,
        )
        turn = await coordinator.send("write")
        reopened = JsonlSession(session_dir)
        return {event.event_type async for event in reopened.read(turn.session_id)}

    event_types = asyncio.run(scenario())

    assert RuntimeEventType.PERMISSION_REQUESTED in event_types
    assert RuntimeEventType.PERMISSION_RESOLVED in event_types
    assert RuntimeEventType.TOOL_EXECUTION_COMPLETED in event_types
    assert RuntimeEventType.TURN_COMPLETED in event_types
    assert not (tmp_path / "note.txt").exists()
