"""Sequential remote Task waiting with durable, non-model handle references."""

import asyncio
import json
from dataclasses import asdict
from uuid import uuid4

from jdagent.data_paths import workspace_identity
from jdagent.domain.events import (
    MCPTaskCancelRequestedPayload,
    MCPTaskObservedPayload,
    MCPTaskRegisteredPayload,
    RuntimeEventType,
)
from jdagent.domain.json import JsonObject
from jdagent.domain.mcp import MCPCapability, MCPFailure, MCPTask, MCPTaskSnapshot
from jdagent.domain.tools import ToolErrorCode, ToolExecutionContext, ToolOutcome
from jdagent.mcp_service import MCPService
from jdagent.ports import ContextObjects, RuntimeJournal


class MCPTaskExecution:
    """Hold the active tool call until its registered Task returns a business result."""

    def __init__(
        self,
        service: MCPService,
        journal: RuntimeJournal,
        objects: ContextObjects,
        *,
        max_control_bytes: int,
    ) -> None:
        self._service, self._journal, self._objects = service, journal, objects
        self._max_control_bytes = max_control_bytes
        self._task: MCPTask | None = None
        self._task_ref: str | None = None
        self._cancel_requested = False
        self._cancel_acknowledged = False
        self.result_confirmed = False
        self.final_result: JsonObject | None = None

    @property
    def details(self) -> JsonObject:
        if self._task_ref is None or self._task is None:
            return {}
        return {
            "task_ref": self._task_ref,
            "task_status": self._task.status,
            "cancellation_requested": self._cancel_requested,
            "cancellation_acknowledged": self._cancel_acknowledged,
        }

    async def wait(
        self, entry: MCPCapability, task: MCPTask, context: ToolExecutionContext
    ) -> JsonObject | ToolOutcome:
        task_ref = uuid4().hex
        control = {
            "task": asdict(task),
            "identity_digest": self._service.task_identity(context.session_id, entry),
            "session_id": context.session_id,
            "turn_id": context.turn_id,
            "call_id": context.call_id,
            "server_id": entry.server_id,
            "workspace_identity": workspace_identity(context.workspace),
            "task_ref": task_ref,
            "definition": asdict(entry),
        }
        encoded = json.dumps(control, ensure_ascii=False, sort_keys=True).encode("utf-8")
        if len(encoded) > self._max_control_bytes:
            raise MCPFailure("response_limit")
        digest = await self._objects.put(encoded)
        await self._journal.record(
            context.turn_id,
            RuntimeEventType.MCP_TASK_REGISTERED,
            MCPTaskRegisteredPayload(
                context.call_id,
                task_ref,
                entry.server_id,
                entry.name,
                entry.generation,
                digest,
                task.status,
                len(encoded),
            ),
        )
        self._task_ref, self._task = task_ref, task
        while True:
            if task.status == "input_required":
                await self.cancel(entry, context)
                return ToolOutcome(
                    data=self.details,
                    completed=False,
                    error_code=ToolErrorCode.UNSUPPORTED,
                    message="MCP Task requires unsupported client input",
                    requires_stop=True,
                )
            # Server hints cannot force busy polling; the outer logical deadline
            # covers this sleep and every query without resetting between them.
            await asyncio.sleep(max(0.1, (task.poll_interval_ms or 0) / 1000))
            snapshot = await self._query(entry, task, task_ref, context)
            task = snapshot.task
            self._task = task
            if task.status == "completed":
                if snapshot.result is None:
                    raise MCPFailure("protocol")
                self.result_confirmed = True
                self.final_result = snapshot.result
                return snapshot.result
            if task.status in {"failed", "cancelled"}:
                self.result_confirmed = True
                return ToolOutcome(
                    data={"task_ref": task_ref, "task_status": task.status},
                    completed=False,
                    observed_failure=True,
                    error_code=ToolErrorCode.CANCELLED
                    if task.status == "cancelled"
                    else ToolErrorCode.EXECUTION_FAILED,
                    message="MCP Task was cancelled remotely"
                    if task.status == "cancelled"
                    else "MCP Task reported failure",
                    cancelled=task.status == "cancelled",
                )

    async def cancel(self, entry: MCPCapability, context: ToolExecutionContext) -> None:
        """Request cancel and inspect its effect within one independent five-second window."""
        if self._task_ref is None or self._task is None or self.result_confirmed:
            return
        task_ref, task = self._task_ref, self._task
        try:
            async with asyncio.timeout(5):
                await self._journal.record(
                    context.turn_id,
                    RuntimeEventType.MCP_TASK_CANCEL_REQUESTED,
                    MCPTaskCancelRequestedPayload(context.call_id, task_ref),
                )
                self._cancel_requested = True
                await self._service.cancel_task(context.session_id, entry, task)
                self._cancel_acknowledged = True
                await self._journal.record(
                    context.turn_id,
                    RuntimeEventType.MCP_TASK_OBSERVED,
                    MCPTaskObservedPayload(context.call_id, task_ref, None, operation="cancel"),
                )
                await asyncio.sleep(max(0.1, (task.poll_interval_ms or 0) / 1000))
                snapshot = await self._query(entry, task, task_ref, context)
                self._task = snapshot.task
                self.result_confirmed = snapshot.task.status in {"completed", "failed", "cancelled"}
                self.final_result = snapshot.result
        except (MCPFailure, TimeoutError, asyncio.CancelledError):
            self._service.quarantine(context.session_id, entry)

    async def _query(
        self, entry: MCPCapability, task: MCPTask, task_ref: str, context: ToolExecutionContext
    ) -> MCPTaskSnapshot:
        for attempt in range(1, 4):
            try:
                snapshot = await self._service.get_task(context.session_id, entry, task)
            except asyncio.CancelledError:
                await self._journal.record(
                    context.turn_id,
                    RuntimeEventType.MCP_TASK_OBSERVED,
                    MCPTaskObservedPayload(
                        context.call_id, task_ref, None, attempt, False, "interrupted"
                    ),
                )
                raise
            except MCPFailure as error:
                await self._journal.record(
                    context.turn_id,
                    RuntimeEventType.MCP_TASK_OBSERVED,
                    MCPTaskObservedPayload(
                        context.call_id,
                        task_ref,
                        None,
                        attempt,
                        error.kind == "server_timeout",
                        error.kind,
                        error.rpc_code,
                    ),
                )
                if error.kind != "server_timeout" or attempt == 3:
                    raise
                # A query retry never re-enters tools/call. It remains under the
                # original logical deadline and also respects the peer's hint.
                await asyncio.sleep(
                    max(0.5 * 2 ** (attempt - 1), (task.poll_interval_ms or 0) / 1000)
                )
            else:
                await self._journal.record(
                    context.turn_id,
                    RuntimeEventType.MCP_TASK_OBSERVED,
                    MCPTaskObservedPayload(
                        context.call_id, task_ref, snapshot.task.status, attempt
                    ),
                )
                return snapshot
        raise RuntimeError("Task query retry allowance was not enforced")
