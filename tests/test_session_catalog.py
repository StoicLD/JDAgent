import asyncio
from pathlib import Path

import pytest

from jdagent.adapters.fake import FakeApproval, FakeModelPort
from jdagent.adapters.jsonl_session import JsonlSession
from jdagent.adapters.legacy_sessions import import_legacy_sessions
from jdagent.adapters.memory import InMemorySession
from jdagent.application.session_catalog import SessionCatalog, SessionSelectionError
from jdagent.domain.events import RuntimeEventType, SessionStartedPayload
from jdagent.domain.model import ResponseCompleted, TextDelta
from jdagent.domain.tools import ApprovalDecision
from jdagent.eventing import EventJournal
from tests.runtime_factory import create_test_coordinator


def test_catalog_rebuilds_names_from_session_facts(tmp_path: Path) -> None:
    async def scenario() -> tuple[str, str, str]:
        session = InMemorySession()
        coordinator = create_test_coordinator(
            model=FakeModelPort(scripts=((TextDelta("answer"), ResponseCompleted("stop")),)),
            session=session,
            tools=(),
            approval=FakeApproval(ApprovalDecision.APPROVE),
            workspace=tmp_path,
            workspace_identity="workspace-1",
        )
        turn = await coordinator.send("question")
        first_catalog = SessionCatalog(
            session=session,
            discovery=session,
            workspace_identity="workspace-1",
        )
        initial = (await first_catalog.list_sessions())[0]
        await first_catalog.rename(turn.session_id, "work")

        rebuilt = SessionCatalog(
            session=session,
            discovery=session,
            workspace_identity="workspace-1",
        )
        renamed = (await rebuilt.list_sessions())[0]
        return initial.name, renamed.name, renamed.session_id

    initial_name, renamed_name, session_id = asyncio.run(scenario())

    assert initial_name.startswith("session-")
    assert renamed_name == "work"
    assert session_id


def test_session_selector_reports_ambiguous_name(tmp_path: Path) -> None:
    async def scenario() -> None:
        session = InMemorySession()
        coordinator = create_test_coordinator(
            model=FakeModelPort(
                scripts=((TextDelta("answer"), ResponseCompleted("stop")),),
                repeat_last=True,
            ),
            session=session,
            tools=(),
            approval=FakeApproval(ApprovalDecision.APPROVE),
            workspace=tmp_path,
            workspace_identity="workspace-1",
        )
        first = await coordinator.send("first")
        second = await coordinator.send("second")
        catalog = SessionCatalog(
            session=session,
            discovery=session,
            workspace_identity="workspace-1",
        )
        await catalog.rename(first.session_id, "duplicate")
        await catalog.rename(second.session_id, "duplicate")

        with pytest.raises(SessionSelectionError, match="ambiguous"):
            await catalog.resolve("duplicate")

    asyncio.run(scenario())


def test_legacy_session_import_never_deletes_source(tmp_path: Path) -> None:
    legacy_directory = tmp_path / "workspace" / ".jdagent" / "sessions"
    target_directory = tmp_path / "user-data" / "sessions"

    async def scenario() -> tuple[tuple[str, ...], tuple[str, ...]]:
        legacy = JsonlSession(legacy_directory)
        journal = await EventJournal.open(legacy, "legacy-session")
        await journal.record(
            None,
            RuntimeEventType.SESSION_STARTED,
            SessionStartedPayload(),
        )
        imported = await import_legacy_sessions(legacy_directory, target_directory)
        target = JsonlSession(target_directory)
        target_events = tuple([event.event_id async for event in target.read("legacy-session")])
        return imported, target_events

    imported_ids, target_event_ids = asyncio.run(scenario())

    assert imported_ids == ("legacy-session",)
    assert target_event_ids
    assert (legacy_directory / "legacy-session.jsonl").exists()
    assert (target_directory / "legacy-session.jsonl").exists()
