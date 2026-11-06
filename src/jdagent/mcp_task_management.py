"""Explicit management of durable tasks without resuming an Agent workflow."""

import asyncio
import json
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Literal

from jdagent.data_paths import workspace_identity
from jdagent.domain.events import (
    MCPInvocationFinishedPayload,
    MCPTaskObservedPayload,
    MCPTaskRegisteredPayload,
    MCPTaskResultSavedPayload,
    RuntimeEvent,
)
from jdagent.domain.json import normalize_json, require_object, require_string
from jdagent.domain.mcp import MCPCapability, MCPFailure, MCPTask, MCPTaskView
from jdagent.domain.tools import ToolErrorCode, ToolExecutionContext, ToolOutcome
from jdagent.mcp_execution import MCPExecution, MCPResultLimits
from jdagent.mcp_service import MCPService
from jdagent.mcp_task_execution import MCPTaskExecution
from jdagent.ports import ContextObjects, RuntimeJournal

MCPTaskAction = Literal["query", "result", "cancel"]


class MCPTaskManagement:
    """Use original Session bindings; management facts do not reopen old Turns."""

    def __init__(
        self,
        service: MCPService,
        objects: ContextObjects,
        limits: Callable[[], MCPResultLimits],
        workspace: Path,
    ) -> None:
        self._service, self._objects, self._limits, self._workspace = (
            service,
            objects,
            limits,
            workspace,
        )

    def list_tasks(self, journal: RuntimeJournal) -> tuple[MCPTaskView, ...]:
        result: list[MCPTaskView] = []
        for event in journal.events:
            registration = event.payload
            if not isinstance(registration, MCPTaskRegisteredPayload):
                continue
            status = registration.status
            timing = registration.timing
            for update in journal.events:
                payload = update.payload
                if (
                    isinstance(payload, MCPTaskObservedPayload)
                    and payload.task_ref == registration.task_ref
                    and payload.status is not None
                ):
                    status = payload.status
                    if payload.timing is not None:
                        timing = payload.timing
            result.append(
                MCPTaskView(
                    registration.task_ref,
                    registration.server_id,
                    registration.raw_name,
                    status,
                    self._saved(journal, event) is not None,
                    timing.expired if timing is not None else None,
                )
            )
        return tuple(result)

    async def act(
        self, journal: RuntimeJournal, session_id: str, task_ref: str, action: MCPTaskAction
    ) -> ToolOutcome:
        limits = self._limits()
        registration = next(
            (
                e
                for e in journal.events
                if isinstance(e.payload, MCPTaskRegisteredPayload)
                and e.payload.task_ref == task_ref
            ),
            None,
        )
        if registration is None:
            return ToolOutcome(
                completed=False,
                error_code=ToolErrorCode.TOOL_UNAVAILABLE,
                message="Task reference does not belong to this Session",
            )
        if action == "result" and (saved := self._saved(journal, registration)) is not None:
            return saved
        assert isinstance(registration.payload, MCPTaskRegisteredPayload)
        record = registration.payload
        control = require_object(
            normalize_json(
                json.loads((await self._objects.get(record.handle_hash)).decode("utf-8"))
            ),
            "Task control object",
        )
        if (
            control.get("session_id") != session_id
            or control.get("task_ref") != task_ref
            or control.get("turn_id") != registration.turn_id
            or control.get("call_id") != record.call_id
            or control.get("server_id") != record.server_id
            or control.get("workspace_identity") != workspace_identity(self._workspace)
        ):
            raise ValueError("Task control binding is inconsistent")
        definition = require_object(control.get("definition"), "Task definition")
        if (
            definition.get("kind") != "tool"
            or definition.get("server_id") != record.server_id
            or definition.get("name") != record.raw_name
        ):
            raise ValueError("Task definition binding is inconsistent")
        entry = MCPCapability(
            record.server_id,
            require_string(definition, "generation"),
            "tool",
            record.raw_name,
            require_string(definition, "description"),
            require_string(definition, "definition_json"),
        )
        raw_task = require_object(control.get("task"), "Task handle")
        task = MCPTask.from_data(
            {
                "taskId": raw_task.get("task_id"),
                "status": raw_task.get("status"),
                "createdAt": raw_task.get("created_at"),
                "ttlMs": raw_task.get("ttl_ms"),
                "pollIntervalMs": raw_task.get("poll_interval_ms"),
            }
        )
        view = next(item for item in self.list_tasks(journal) if item.task_ref == task_ref)
        task = replace(task, status=view.status)
        for event in journal.events:
            update = event.payload
            if (
                isinstance(update, MCPTaskObservedPayload)
                and update.task_ref == task_ref
                and update.timing is not None
            ):
                task = replace(
                    task,
                    created_at=update.timing.created_at,
                    ttl_ms=update.timing.ttl_ms,
                    poll_interval_ms=update.timing.poll_interval_ms,
                )
        context = ToolExecutionContext(
            session_id, require_string(control, "turn_id"), self._workspace, call_id=record.call_id
        )
        execution = MCPTaskExecution(
            self._service,
            journal,
            self._objects,
            max_control_bytes=limits.reservation_bytes,
            management=True,
        )
        execution.attach(task_ref, task)
        original_entry = entry
        interruption: ToolOutcome | None = None
        try:
            if task.timing.expired:
                raise MCPFailure("task_expired")
            entry = await self._service.bind_task(
                session_id, entry, require_string(control, "identity_digest")
            )
            self._service.begin_activity(
                session_id,
                entry,
                "tasks/cancel" if action == "cancel" else "tasks/get",
                task_status=task.status,
            )
            if action == "cancel":
                await execution.cancel(entry, context)
                if execution.control_failure is not None:
                    raise execution.control_failure
            else:
                async with asyncio.timeout(self._service.execution_timeout(session_id, entry)):
                    snapshot = await execution.query(entry, context)
                    if snapshot.task.status == "input_required":
                        await execution.cancel(entry, context)
                        interruption = ToolOutcome(
                            data=execution.details,
                            completed=False,
                            error_code=ToolErrorCode.UNSUPPORTED,
                            message="MCP Task requires unsupported client input",
                        )
        except asyncio.CancelledError:
            if execution.final_result is None:
                raise
            # The reply may already be in hand while its observation is being
            # committed. Keep that result before returning local cancellation.
            interruption = ToolOutcome(
                data=execution.details,
                completed=False,
                cancelled=True,
                requires_stop=True,
                error_code=ToolErrorCode.CANCELLED,
                message="MCP management waiting cancelled; remote result was received",
            )
        except (MCPFailure, TimeoutError) as error:
            reason = error.kind if isinstance(error, MCPFailure) else "client_timeout"
            interruption = ToolOutcome(
                data={**execution.details, "reason": reason},
                completed=False,
                error_code=ToolErrorCode.TOOL_UNAVAILABLE,
                message=f"Original MCP Task could not be queried: {reason}",
            )
        finally:
            self._service.end_activity(session_id, entry)
        if execution.final_result is not None:
            saved = await MCPExecution(
                self._service, journal, self._objects, limits
            ).publish_task_result(
                original_entry, execution.final_result, context, execution.details
            )
            # Keep the business result separately from this management action's
            # unsupported-input or cleanup failure, so later result reads work.
            return (
                saved
                if interruption is None
                else replace(
                    interruption,
                    data={
                        **saved.data,
                        **interruption.data,
                        "result_received_during_cleanup": True,
                    },
                )
            )
        if interruption is not None:
            return interruption
        status = execution.details.get("task_status")
        return ToolOutcome(
            data=execution.details,
            completed=False,
            observed_failure=status in {"failed", "cancelled"},
            cancelled=status == "cancelled",
            error_code=ToolErrorCode.EXECUTION_FAILED
            if status == "failed"
            else ToolErrorCode.CANCELLED
            if status == "cancelled"
            else None,
            message="Remote Task is still unconfirmed"
            if not execution.result_confirmed
            else "Remote Task is terminal",
        )

    @staticmethod
    def _saved(journal: RuntimeJournal, registration: RuntimeEvent) -> ToolOutcome | None:
        assert isinstance(registration.payload, MCPTaskRegisteredPayload)
        record = registration.payload
        for event in reversed(journal.events):
            payload = event.payload
            if (
                isinstance(payload, MCPTaskResultSavedPayload)
                and payload.task_ref == record.task_ref
            ):
                return payload.outcome
            if (
                isinstance(payload, MCPInvocationFinishedPayload)
                and event.turn_id == registration.turn_id
                and payload.result.call_id == record.call_id
                and payload.original_hash is not None
            ):
                return payload.result.outcome
        return None
