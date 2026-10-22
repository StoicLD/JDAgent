import asyncio
import threading
from pathlib import Path

import pytest

import jdagent.adapters.jsonl_recovery as recovery_adapter
import jdagent.adapters.jsonl_session as session_adapter
from jdagent.adapters.jsonl_recovery import JsonlRecoveryStore
from jdagent.adapters.jsonl_session import JsonlSession
from jdagent.adapters.session_lock import SessionLease
from jdagent.application.session_recovery import SessionRecovery, prepare_session_resume
from jdagent.domain.errors import SessionError, SessionErrorCode
from jdagent.domain.events import (
    RuntimeEvent,
    RuntimeEventType,
    SessionStartedPayload,
    ToolExecutionStartedPayload,
    UserMessagePayload,
)
from jdagent.domain.tools import RiskLevel
from jdagent.eventing import EventJournal


@pytest.mark.parametrize("already_owned", [False, True])
def test_parent_stays_exclusive_until_recovery_snapshot_is_durable(
    tmp_path: Path, already_owned: bool
):
    async def scenario():
        competitor = JsonlSession(tmp_path)

        class ObservedSession(JsonlSession):
            async def append(self, event: RuntimeEvent) -> None:
                if event.event_type is RuntimeEventType.RECOVERY_SNAPSHOT:
                    try:
                        with pytest.raises(SessionError) as denied:
                            await competitor.acquire("parent")
                        assert denied.value.code is SessionErrorCode.BUSY
                    finally:
                        await competitor.release("parent")
                await super().append(event)

        session = ObservedSession(tmp_path)
        journal = await EventJournal.open(session, "parent")
        await journal.record(
            None, RuntimeEventType.SESSION_STARTED, SessionStartedPayload(workspace_identity="w")
        )
        await journal.record("t", RuntimeEventType.USER_MESSAGE, UserMessagePayload("write file"))
        await journal.record(
            "t",
            RuntimeEventType.TOOL_EXECUTION_STARTED,
            ToolExecutionStartedPayload("write", "write_text_file", RiskLevel.WRITE),
        )
        before = session.path_for("parent").read_bytes()
        if already_owned:
            await session.acquire("parent")
        try:
            prepared = await prepare_session_resume(
                session=session,
                recovery=SessionRecovery(JsonlRecoveryStore(tmp_path, session)),
                session_id="parent",
                workspace_identity="w",
                allow_recovery_snapshot=True,
            )
            assert prepared.session_id != "parent"
            assert session.path_for("parent").read_bytes() == before
            assert session.owns("parent") is already_owned
        finally:
            await session.release("parent")
        await competitor.acquire("parent")
        await competitor.release("parent")

    asyncio.run(scenario())


@pytest.mark.parametrize("pause_at", ["acquire", "repair", "snapshot"])
def test_cancelled_recovery_retains_lock_until_inflight_storage_finishes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, pause_at: str
):
    entered = threading.Event()
    release = threading.Event()
    armed = False

    def pause():
        entered.set()
        assert release.wait(5)

    class PausedLease(SessionLease):
        def __init__(self, path: Path) -> None:
            super().__init__(path)
            if armed and pause_at == "acquire" and path.stem == "parent":
                pause()

    original_repair = recovery_adapter._repair_locked  # pyright: ignore[reportPrivateUsage]
    original_append = session_adapter.JsonlSession._append_sync  # pyright: ignore[reportPrivateUsage]

    def repair(directory: Path, session_id: str, path: Path):
        if armed and pause_at == "repair":
            pause()
        return original_repair(directory, session_id, path)

    def append(session: JsonlSession, path: Path, event: RuntimeEvent):
        if (
            armed
            and pause_at == "snapshot"
            and event.event_type is RuntimeEventType.RECOVERY_SNAPSHOT
        ):
            pause()
        original_append(session, path, event)

    monkeypatch.setattr(session_adapter, "SessionLease", PausedLease)
    monkeypatch.setattr(recovery_adapter, "_repair_locked", repair)
    monkeypatch.setattr(JsonlSession, "_append_sync", append)

    async def scenario():
        nonlocal armed
        session = JsonlSession(tmp_path)
        journal = await EventJournal.open(session, "parent")
        await journal.record(
            None, RuntimeEventType.SESSION_STARTED, SessionStartedPayload(workspace_identity="w")
        )
        await journal.record("t", RuntimeEventType.USER_MESSAGE, UserMessagePayload("write file"))
        await journal.record(
            "t",
            RuntimeEventType.TOOL_EXECUTION_STARTED,
            ToolExecutionStartedPayload("write", "write_text_file", RiskLevel.WRITE),
        )
        armed = True
        task = asyncio.create_task(
            prepare_session_resume(
                session=session,
                recovery=SessionRecovery(JsonlRecoveryStore(tmp_path, session)),
                session_id="parent",
                workspace_identity="w",
                allow_recovery_snapshot=True,
            )
        )
        competitor = JsonlSession(tmp_path)
        try:
            assert await asyncio.to_thread(entered.wait, 3)
            task.cancel()
            await asyncio.sleep(0.03)
            task.cancel()
            await asyncio.sleep(0.01)
            assert not task.done(), "Cancellation must wait for the owned storage transaction"
            with pytest.raises(SessionError) as denied:
                await competitor.acquire("parent")
            assert denied.value.code is SessionErrorCode.BUSY
        finally:
            armed = False
            release.set()
            await asyncio.gather(task, return_exceptions=True)
            retained_after_cancel = session.owns("parent")
            await session.release("parent")
            await competitor.release("parent")
        assert task.cancelled()
        assert not retained_after_cancel
        await competitor.acquire("parent")
        await competitor.release("parent")

    asyncio.run(scenario())
