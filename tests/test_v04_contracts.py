import asyncio
from dataclasses import replace
from pathlib import Path

import pytest

from jdagent.adapters.fake import FakeApproval, FakeModelPort
from jdagent.adapters.jsonl_session import event_from_data, event_to_data
from jdagent.adapters.memory import InMemorySession
from jdagent.application.session_recovery import (
    LogicalRecovery,
    classify_logical_recovery,
    close_interrupted_turn,
)
from jdagent.context import ContextBuilder, ContextLimitError, project_messages
from jdagent.core.loop import AgentLoop, LoopLimits
from jdagent.domain.errors import StopReason
from jdagent.domain.events import (
    AssistantMessageCompletedPayload,
    RecoverySnapshotPayload,
    RuntimeEvent,
    RuntimeEventType,
    SessionStartedPayload,
    ToolExecutionStartedPayload,
    TurnCompletedPayload,
    UserMessagePayload,
)
from jdagent.domain.json import JsonObject
from jdagent.domain.model import (
    MessageRole,
    ModelCapabilities,
    ModelMessage,
    ResponseCompleted,
    ToolCallCompleted,
)
from jdagent.domain.tools import (
    ApprovalDecision,
    RiskLevel,
    ToolCall,
    ToolCapability,
    ToolDefinition,
    ToolErrorCode,
    ToolExecutionContext,
    ToolOutcome,
)
from jdagent.eventing import EventJournal
from jdagent.tools.builtins import create_builtin_tools
from jdagent.tools.command_output import CommandLogs
from jdagent.tools.commands import create_command_tools
from jdagent.tools.files import create_file_tools
from jdagent.tools.permissions import DefaultPermissionPolicy
from jdagent.tools.runtime import ToolRegistry, ToolRuntime
from jdagent.tools.workspace import WorkspacePathResolver


@pytest.mark.parametrize(
    "name",
    [
        "find_files",
        "read_file",
        "search_text",
        "create_file",
        "edit_file",
        "run_command",
        "read_command_output",
    ],
)
def test_malformed_result_is_rejected_without_losing_side_effect_fact(
    tmp_path: Path, name: str
) -> None:
    definitions = (
        *create_file_tools(tmp_path),
        *create_command_tools(tmp_path, CommandLogs(tmp_path / "logs")),
    )
    definition = next(tool for tool in definitions if tool.name == name)
    side_effect = definition.risk in {RiskLevel.WRITE, RiskLevel.EXECUTE}

    async def malformed(args: JsonObject, context: ToolExecutionContext) -> ToolOutcome:
        return ToolOutcome(data={"unexpected": True}, effect="committed" if side_effect else "none")

    runtime = ToolRuntime(
        ToolRegistry(
            (
                replace(
                    definition, input_schema={"type": "object"}, preflight=None, handler=malformed
                ),
            )
        ),
        DefaultPermissionPolicy(),
        FakeApproval(ApprovalDecision.APPROVE),
    )
    result = asyncio.run(
        runtime.execute(ToolCall("c", name, {}), ToolExecutionContext("s", "t", tmp_path))
    )
    assert result.error_code is ToolErrorCode.EXECUTION_FAILED
    assert result.outcome is not None
    assert result.outcome.effect == ("committed" if side_effect else "none")
    assert result.stop_turn == side_effect


def test_safe_process_recovery_closes_pending_tool_messages(tmp_path: Path) -> None:
    async def scenario() -> None:
        session = InMemorySession()
        journal = await EventJournal.open(session, "s")
        await journal.record(None, RuntimeEventType.SESSION_STARTED, SessionStartedPayload())
        await journal.record("t", RuntimeEventType.USER_MESSAGE, UserMessagePayload("read"))
        calls = (ToolCall("a", "read_file", {}), ToolCall("b", "read_file", {}))
        await journal.record(
            "t",
            RuntimeEventType.ASSISTANT_MESSAGE_COMPLETED,
            AssistantMessageCompletedPayload("", calls),
        )
        await journal.record(
            "t",
            RuntimeEventType.TOOL_EXECUTION_STARTED,
            ToolExecutionStartedPayload("a", "read_file", RiskLevel.READ),
        )
        await close_interrupted_turn(session=session, events=journal.events)
        reopened = await EventJournal.open(session, "s")
        messages = project_messages(reopened.events)
        assert [
            message.tool_call_id for message in messages if message.role is MessageRole.TOOL
        ] == ["a", "b"]
        assert classify_logical_recovery(reopened.events) is LogicalRecovery.CLEAN

    asyncio.run(scenario())


@pytest.mark.parametrize("failed_without_answer", [False, True])
def test_snapshot_history_is_trimmed_by_whole_turn_without_mutating_facts(
    failed_without_answer: bool,
) -> None:
    history = (
        ModelMessage(MessageRole.USER, "old" * 1000),
        *(
            (ModelMessage(MessageRole.ASSISTANT, "old answer"),)
            if not failed_without_answer
            else ()
        ),
        ModelMessage(MessageRole.USER, "recent"),
        ModelMessage(MessageRole.ASSISTANT, "recent answer"),
    )
    events = [
        RuntimeEvent.create(
            session_id="s",
            turn_id=None,
            sequence=1,
            event_type=RuntimeEventType.RECOVERY_SNAPSHOT,
            payload=RecoverySnapshotPayload("parent", 10, history),
        ),
        RuntimeEvent.create(
            session_id="s",
            turn_id="now",
            sequence=2,
            event_type=RuntimeEventType.USER_MESSAGE,
            payload=UserMessagePayload("current"),
        ),
    ]
    request = ContextBuilder(model="fake", max_context_tokens=150, sliding_window=True).build(
        events, ModelCapabilities()
    )
    assert [message.content for message in request.messages] == [
        "recent",
        "recent answer",
        "current",
    ]
    assert isinstance(events[0].payload, RecoverySnapshotPayload)
    assert events[0].payload.messages == history
    assert "snapshot:" in request.metadata["omitted_turn_ids"]


def test_snapshot_notice_uses_explicit_protection_and_survives_codec() -> None:
    notice = ModelMessage(MessageRole.USER, "Recovery: inspect uncertain output")
    event = RuntimeEvent.create(
        session_id="s",
        turn_id=None,
        sequence=1,
        event_type=RuntimeEventType.RECOVERY_SNAPSHOT,
        payload=RecoverySnapshotPayload(
            "parent", 3, (ModelMessage(MessageRole.USER, "failed task" * 500), notice), 1
        ),
    )
    event = event_from_data(event_to_data(event))
    current = RuntimeEvent.create(
        session_id="s",
        turn_id="now",
        sequence=2,
        event_type=RuntimeEventType.USER_MESSAGE,
        payload=UserMessagePayload("current"),
    )
    request = ContextBuilder(model="fake", max_context_tokens=120, sliding_window=True).build(
        [event, current], ModelCapabilities()
    )
    assert request.messages == (notice, ModelMessage(MessageRole.USER, "current"))


def test_repeated_cancellation_keeps_cleanup_owned(tmp_path: Path) -> None:
    async def scenario() -> None:
        started, cleaning, release, finished = (asyncio.Event() for _ in range(4))

        async def handler(args: JsonObject, context: ToolExecutionContext) -> ToolOutcome:
            started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cleaning.set()
                await release.wait()
                finished.set()
                return ToolOutcome(effect="unknown", completed=False)
            raise AssertionError("Unreachable")

        runtime = ToolRuntime(
            ToolRegistry(
                (ToolDefinition("write", "write", {"type": "object"}, RiskLevel.WRITE, handler),)
            ),
            DefaultPermissionPolicy(),
            FakeApproval(ApprovalDecision.APPROVE),
        )
        running = asyncio.create_task(
            runtime.execute(ToolCall("c", "write", {}), ToolExecutionContext("s", "t", tmp_path))
        )
        await started.wait()
        running.cancel()
        await cleaning.wait()
        running.cancel()
        await asyncio.sleep(0)
        assert not running.done()
        release.set()
        result = await running
        assert finished.is_set()
        assert result.stop_turn

    asyncio.run(scenario())


def test_rejected_invocation_does_not_reprompt_in_same_turn(tmp_path: Path) -> None:
    async def scenario() -> None:
        approval = FakeApproval(ApprovalDecision.REJECT)
        runtime = ToolRuntime(
            ToolRegistry(create_builtin_tools(WorkspacePathResolver(tmp_path))),
            DefaultPermissionPolicy(),
            approval,
        )
        for call_id, turn_id in (("a", "t"), ("b", "t"), ("c", "next")):
            result = await runtime.execute(
                ToolCall(call_id, "write_text_file", {"path": "new.txt", "content": "hello"}),
                ToolExecutionContext("s", turn_id, tmp_path),
            )
            assert result.error_code is ToolErrorCode.APPROVAL_REJECTED
        assert len(approval.requests) == 2
        assert not (tmp_path / "new.txt").exists()

    asyncio.run(scenario())


def test_unavailable_tool_cannot_execute_even_when_registered(tmp_path: Path) -> None:
    registry = ToolRegistry(create_builtin_tools(WorkspacePathResolver(tmp_path)))
    runtime = ToolRuntime(
        registry,
        DefaultPermissionPolicy(),
        FakeApproval(ApprovalDecision.APPROVE),
        capability=ToolCapability.ANALYZE,
    )
    result = asyncio.run(
        runtime.execute(
            ToolCall("create", "write_text_file", {"path": "out.txt", "content": "no"}),
            ToolExecutionContext("session", "turn", tmp_path),
        )
    )
    assert result.error_code is not None
    assert result.error_code.value == "tool_unavailable"
    assert not (tmp_path / "out.txt").exists()


def test_stop_pairs_pending_calls_without_executing_them(tmp_path: Path) -> None:
    executed = 0

    async def handler(args: JsonObject, context: ToolExecutionContext) -> ToolOutcome:
        nonlocal executed
        executed += 1
        return ToolOutcome(completed=False, effect="unknown", error_code=ToolErrorCode.UNCERTAIN)

    async def scenario() -> None:
        tool = ToolDefinition("custom", "custom tool", {"type": "object"}, RiskLevel.WRITE, handler)
        journal = await EventJournal.open(InMemorySession(), "s")
        await journal.record(None, RuntimeEventType.SESSION_STARTED, SessionStartedPayload())
        await journal.record("t", RuntimeEventType.USER_MESSAGE, UserMessagePayload("run"))
        model = FakeModelPort(
            scripts=(
                (
                    ToolCallCompleted(ToolCall("a", "custom", {})),
                    ToolCallCompleted(ToolCall("b", "custom", {})),
                    ResponseCompleted("tool_calls"),
                ),
            )
        )
        runtime = ToolRuntime(
            ToolRegistry((tool,)),
            DefaultPermissionPolicy(),
            FakeApproval(ApprovalDecision.APPROVE),
            recorder=journal,
        )
        result = await AgentLoop(
            model, ContextBuilder(model="fake", tools=(tool,)), runtime, journal
        ).run("t", ToolExecutionContext("s", "t", tmp_path))
        assert result.stop_reason is StopReason.LIMIT_REACHED
        assert executed == 1
        messages = project_messages(journal.events)
        assert [
            message.tool_call_id for message in messages if message.role is MessageRole.TOOL
        ] == ["a", "b"]
        assert (
            classify_logical_recovery(tuple(journal.events))
            is LogicalRecovery.UNCERTAIN_SIDE_EFFECT
        )
        for event in journal.events:
            assert event_from_data(event_to_data(event)) == event

    asyncio.run(scenario())


def test_sliding_window_drops_only_complete_old_turns_and_keeps_events() -> None:
    events = [
        RuntimeEvent.create(
            session_id="s",
            turn_id="old",
            sequence=1,
            event_type=RuntimeEventType.USER_MESSAGE,
            payload=UserMessagePayload("old" * 1000),
        )
    ]
    events.append(
        RuntimeEvent.create(
            session_id="s",
            turn_id="old",
            sequence=2,
            event_type=RuntimeEventType.TURN_COMPLETED,
            payload=TurnCompletedPayload(StopReason.COMPLETED, 1, 0),
        )
    )
    events.append(
        RuntimeEvent.create(
            session_id="s",
            turn_id="now",
            sequence=3,
            event_type=RuntimeEventType.USER_MESSAGE,
            payload=UserMessagePayload("current task"),
        )
    )
    request = ContextBuilder(model="fake", max_context_tokens=100, sliding_window=True).build(
        events, ModelCapabilities()
    )
    assert [message.content for message in request.messages] == ["current task"]
    assert request.metadata["omitted_turn_ids"] == "old"
    assert len(events) == 3
    with pytest.raises(ContextLimitError):
        ContextBuilder(model="fake", max_context_tokens=10, sliding_window=True).build(
            events, ModelCapabilities()
        )


def test_failure_recovery_budget_stops_after_two_retries(tmp_path: Path) -> None:
    async def handler(args: JsonObject, context: ToolExecutionContext) -> ToolOutcome:
        return ToolOutcome(data={"exit_code": 1}, observed_failure=True, effect="possible")

    async def scenario() -> None:
        tool = ToolDefinition("verify", "test tool", {"type": "object"}, RiskLevel.PURE, handler)
        journal = await EventJournal.open(InMemorySession(), "s")
        await journal.record(None, RuntimeEventType.SESSION_STARTED, SessionStartedPayload())
        await journal.record("t", RuntimeEventType.USER_MESSAGE, UserMessagePayload("verify"))
        model = FakeModelPort(
            scripts=tuple(
                (ToolCallCompleted(ToolCall(str(i), "verify", {})), ResponseCompleted("tool_calls"))
                for i in range(4)
            )
        )
        result = await AgentLoop(
            model,
            ContextBuilder(model="fake", tools=(tool,)),
            ToolRuntime(
                ToolRegistry((tool,)),
                DefaultPermissionPolicy(),
                FakeApproval(ApprovalDecision.APPROVE),
            ),
            journal,
            limits=LoopLimits(),
        ).run("t", ToolExecutionContext("s", "t", tmp_path))
        assert result.error_category == "failure_recovery_limit"
        assert result.tool_calls == 3
        assert result.model_calls == 3

    asyncio.run(scenario())
