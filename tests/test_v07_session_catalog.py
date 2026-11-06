"""Unreadable sessions remain isolated in the public catalog and CLI."""

import asyncio
import json
from pathlib import Path

import pytest

from jdagent.adapters.fake import FakeApproval, FakeModelPort
from jdagent.adapters.jsonl_recovery import JsonlRecoveryStore
from jdagent.adapters.jsonl_session import JsonlSession
from jdagent.adapters.scripted_terminal import CapturePresenter, ScriptedTerminal
from jdagent.application.interactive import (
    CommandAction,
    CommandName,
    ExitAction,
    InteractiveApplication,
    InteractiveContext,
    PromptAction,
)
from jdagent.application.session_catalog import SessionCatalog
from jdagent.application.session_recovery import SessionRecovery
from jdagent.domain.errors import SessionError, SessionErrorCode, StopReason
from jdagent.domain.events import RuntimeEventType, SessionStartedPayload, TurnCompletedPayload
from jdagent.domain.model import ResponseCompleted, TextDelta
from jdagent.domain.tools import ApprovalDecision
from jdagent.eventing import EventJournal
from tests.runtime_factory import create_test_coordinator


async def populate_catalog(directory: Path) -> dict[str, bytes]:
    session = JsonlSession(directory)
    try:
        for session_id in ("readable", "unsupported", "unknown-event", "corrupted"):
            journal = await EventJournal.open(session, session_id)
            await journal.record(
                None,
                RuntimeEventType.SESSION_STARTED,
                SessionStartedPayload(session_id, "workspace-1"),
            )
            await journal.record(
                "turn",
                RuntimeEventType.TURN_COMPLETED,
                TurnCompletedPayload(StopReason.COMPLETED, 1, 0),
            )
    finally:
        for session_id in await session.list_session_ids():
            await session.release(session_id)
    originals: dict[str, bytes] = {}
    for session_id in ("unsupported", "unknown-event", "corrupted"):
        path = session.path_for(session_id)
        records = [json.loads(line) for line in path.read_bytes().splitlines()]
        if session_id == "unsupported":
            records[0]["schema_version"] = 999
        elif session_id == "unknown-event":
            records[1]["event_type"] = "mcp_future_execution_fact"
        raw = b"".join(json.dumps(record).encode() + b"\n" for record in records)
        if session_id == "corrupted":
            raw += b'{"private-corruption-detail": invalid}\n'
        path.write_bytes(raw)
        originals[session_id] = raw
    return originals


@pytest.mark.parametrize("use_recovery", [True, False])
def test_one_unreadable_session_does_not_hide_other_sessions_or_allow_unsafe_resume(
    tmp_path: Path, use_recovery: bool
) -> None:
    async def scenario() -> None:
        directory = tmp_path / "sessions"
        originals = await populate_catalog(directory)
        session = JsonlSession(directory)
        catalog = SessionCatalog(
            session=session,
            discovery=session,
            workspace_identity="workspace-1",
            recovery=SessionRecovery(JsonlRecoveryStore(directory, session))
            if use_recovery
            else None,
        )
        try:
            listing = await catalog.list_sessions()
            assert len(listing) == 4 and listing[0].session_id == "readable"
            assert listing[0].name == "readable" and listing[0].status == "completed"
            assert listing[0].updated_at is not None and listing[0].read_error is None
            for item in listing[1:]:
                expected = (
                    SessionErrorCode.UNSUPPORTED_SCHEMA
                    if item.session_id == "unsupported"
                    else SessionErrorCode.CORRUPT_EVENT
                )
                assert item.status == expected.value and item.read_error is expected
                assert (
                    item.name is None
                    and item.updated_at is None
                    and item.workspace_identity is None
                )
                with pytest.raises(SessionError) as error:
                    await catalog.resolve(item.session_id)
                assert error.value.code is expected
                assert session.path_for(item.session_id).read_bytes() == originals[item.session_id]
            assert (await catalog.resolve("readable")).status == "completed"
        finally:
            for session_id in await session.list_session_ids():
                await session.release(session_id)

    asyncio.run(scenario())


def test_cli_lists_unreadable_statuses_and_still_resumes_a_valid_session(tmp_path: Path) -> None:
    async def scenario() -> None:
        directory = tmp_path / "sessions"
        originals = await populate_catalog(directory)
        session = JsonlSession(directory)
        recovery = SessionRecovery(JsonlRecoveryStore(directory, session))
        model = FakeModelPort(scripts=((TextDelta("continued"), ResponseCompleted("stop")),))
        coordinator = create_test_coordinator(
            model=model,
            session=session,
            tools=(),
            approval=FakeApproval(ApprovalDecision.APPROVE),
            workspace=tmp_path,
            workspace_identity="workspace-1",
        )
        presenter = CapturePresenter()
        application = InteractiveApplication(
            terminal=ScriptedTerminal(
                (
                    CommandAction(CommandName.SESSIONS),
                    CommandAction(CommandName.RESUME, ("unsupported",)),
                    CommandAction(CommandName.RESUME, ("unknown-event",)),
                    CommandAction(CommandName.RESUME, ("readable",)),
                    PromptAction("Continue the readable session"),
                    ExitAction(),
                )
            ),
            presenter=presenter,
            coordinator=coordinator,
            catalog=SessionCatalog(
                session=session,
                discovery=session,
                workspace_identity="workspace-1",
                recovery=recovery,
            ),
            session=session,
            context=InteractiveContext("fake", "fake", tmp_path, "ask"),
            recovery=recovery,
            workspace_identity="workspace-1",
        )
        try:
            assert await application.run() == 0
            messages = "\n".join(event.message for event in presenter.events)
            assert "unsupported_schema" in messages and "corrupt_event" in messages
            assert "updated_at=unknown" in messages and "workspace=unknown" in messages
            assert "resumed readable" in messages and "continued" in messages
            assert "private-corruption-detail" not in messages and len(model.requests) == 1
            for session_id, raw in originals.items():
                assert session.path_for(session_id).read_bytes() == raw
        finally:
            await coordinator.aclose()

    asyncio.run(scenario())
