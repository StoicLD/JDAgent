"""Sequential remote Task waiting with durable, non-model handle references."""

import asyncio
from datetime import UTC, datetime
from uuid import uuid4

from jdagent.cancellation import complete_before_cancel
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
from jdagent.mcp_task_binding import MCPTaskBinding
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
        management: bool = False,
    ) -> None:
        self._service, self._journal, self._objects = service, journal, objects
        self._max_control_bytes = max_control_bytes
        self._management = management
        self._task: MCPTask | None = None
        self._task_ref: str | None = None
        self._cancel_requested = False
        self._cancel_acknowledged = False
        self.result_confirmed = False
        self.final_result: JsonObject | None = None
        self.control_failure: MCPFailure | None = None

    def attach(self, task_ref: str, task: MCPTask) -> None:
        """Use a verified durable binding without issuing a new business operation."""
        self._task_ref, self._task = task_ref, task

    async def query(self, entry: MCPCapability, context: ToolExecutionContext) -> MCPTaskSnapshot:
        if self._task_ref is None or self._task is None:
            raise MCPFailure("configuration")
        previous = next(
            (
                event.timestamp
                for event in reversed(self._journal.events)
                if isinstance(event.payload, (MCPTaskRegisteredPayload, MCPTaskObservedPayload))
                and event.payload.task_ref == self._task_ref
            ),
            None,
        )
        interval = max(0.1, (self._task.poll_interval_ms or 0) / 1000)
        # Explicit management also obeys the latest durable hint after restart.
        # A backwards wall clock delays at most one full interval.
        elapsed = 0 if previous is None else max(0, (datetime.now(UTC) - previous).total_seconds())
        if elapsed < interval:
            await asyncio.sleep(interval - elapsed)
        snapshot = await self._query(entry, self._task, self._task_ref, context)
        self._task = snapshot.task
        self.result_confirmed = snapshot.task.status in {"completed", "failed", "cancelled"}
        self.final_result = snapshot.result
        return snapshot

    @property
    def details(self) -> JsonObject:
        if self._task_ref is None or self._task is None:
            return {}
        return {
            "task_ref": self._task_ref,
            "task_status": self._task.status,
            "cancellation_requested": self._cancel_requested,
            "cancellation_acknowledged": self._cancel_acknowledged,
            "reference_expired": self._task.timing.expired,
            "result_confirmed": self.result_confirmed,
        }

    async def register(
        self, entry: MCPCapability, task: MCPTask, context: ToolExecutionContext
    ) -> None:
        """Publish the received handle before entering cancellable Task polling."""
        await complete_before_cancel(self._register(entry, task, context))

    async def _register(
        self, entry: MCPCapability, task: MCPTask, context: ToolExecutionContext
    ) -> None:
        task_ref = uuid4().hex
        encoded = MCPTaskBinding(
            task=task,
            identity_digest=self._service.task_identity(context.session_id, entry),
            session_id=context.session_id,
            turn_id=context.turn_id,
            call_id=context.call_id,
            workspace_identity=workspace_identity(context.workspace),
            task_ref=task_ref,
            definition=entry,
        ).to_bytes()
        if len(encoded) > self._max_control_bytes:
            raise MCPFailure("response_limit")
        digest = await self._objects.put(encoded)
        # Keep the received handle available if cancellation arrives while its
        # durable registration finishes or observers receive that event.
        self._task_ref, self._task = task_ref, task
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
                task.timing,
            ),
        )

    async def wait(
        self, entry: MCPCapability, task: MCPTask, context: ToolExecutionContext
    ) -> JsonObject | ToolOutcome:
        task_ref = self._task_ref
        if task_ref is None:
            raise MCPFailure("configuration")
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

    async def cancel(
        self, entry: MCPCapability, context: ToolExecutionContext, *, query_result: bool = True
    ) -> None:
        """Request cancel and inspect its effect within one independent five-second window."""
        if self._task_ref is None or self._task is None or self.result_confirmed:
            return
        task_ref, task = self._task_ref, self._task
        if task.timing.expired:
            return
        try:
            async with asyncio.timeout(5):
                await self._journal.record(
                    None if self._management else context.turn_id,
                    RuntimeEventType.MCP_TASK_CANCEL_REQUESTED,
                    MCPTaskCancelRequestedPayload(context.call_id, task_ref),
                )
                self._cancel_requested = True
                await self._service.cancel_task(context.session_id, entry, task)
                self._cancel_acknowledged = True
                await self._journal.record(
                    None if self._management else context.turn_id,
                    RuntimeEventType.MCP_TASK_OBSERVED,
                    MCPTaskObservedPayload(context.call_id, task_ref, None, operation="cancel"),
                )
                if not query_result:
                    return
                await asyncio.sleep(max(0.1, (task.poll_interval_ms or 0) / 1000))
                snapshot = await self._query(entry, task, task_ref, context)
                self._task = snapshot.task
                self.result_confirmed = snapshot.task.status in {"completed", "failed", "cancelled"}
                self.final_result = snapshot.result
        except (MCPFailure, TimeoutError, asyncio.CancelledError) as error:
            self.control_failure = (
                error
                if isinstance(error, MCPFailure)
                else MCPFailure(
                    "cleanup_timeout" if isinstance(error, TimeoutError) else "interrupted"
                )
            )
            if not self._cancel_acknowledged:
                await self._journal.record(
                    None if self._management else context.turn_id,
                    RuntimeEventType.MCP_TASK_OBSERVED,
                    MCPTaskObservedPayload(
                        context.call_id,
                        task_ref,
                        None,
                        response_confirmed=isinstance(error, MCPFailure)
                        and error.rpc_code is not None,
                        failure_kind=self.control_failure.kind,
                        rpc_code=self.control_failure.rpc_code,
                        operation="cancel",
                    ),
                )
            self._service.quarantine(context.session_id, entry)

    async def _query(
        self, entry: MCPCapability, task: MCPTask, task_ref: str, context: ToolExecutionContext
    ) -> MCPTaskSnapshot:
        for attempt in range(1, 4):
            try:
                if task.timing.expired:
                    raise MCPFailure("task_expired")
                snapshot = await self._service.get_task(context.session_id, entry, task)
            except asyncio.CancelledError:
                await self._journal.record(
                    None if self._management else context.turn_id,
                    RuntimeEventType.MCP_TASK_OBSERVED,
                    MCPTaskObservedPayload(
                        context.call_id, task_ref, None, attempt, False, "interrupted"
                    ),
                )
                raise
            except MCPFailure as error:
                await self._journal.record(
                    None if self._management else context.turn_id,
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
                # A completed response must survive cancellation at the event
                # boundary; cleanup must not query or cancel the old state.
                self._task = snapshot.task
                self.result_confirmed = snapshot.task.status in {"completed", "failed", "cancelled"}
                self.final_result = snapshot.result
                await self._journal.record(
                    None if self._management else context.turn_id,
                    RuntimeEventType.MCP_TASK_OBSERVED,
                    MCPTaskObservedPayload(
                        context.call_id,
                        task_ref,
                        snapshot.task.status,
                        attempt,
                        timing=snapshot.task.timing,
                    ),
                )
                return snapshot
        raise RuntimeError("Task query retry allowance was not enforced")
