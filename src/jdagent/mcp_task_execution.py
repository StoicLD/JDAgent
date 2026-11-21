"""顺序等待远程任务，将恢复所需的句柄保存在模型之外。"""

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
    """为当前工具调用跟踪已登记的远端长任务，直到得到业务结果或本地等待结束。"""

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
        """接管已校验的任务关联，使后续查询继续使用原任务。"""
        self._task_ref, self._task = task_ref, task

    async def query(self, entry: MCPCapability, context: ToolExecutionContext) -> MCPTaskSnapshot:
        """查询已登记任务并记录观察到的状态，让重启后的管理操作仍有依据。"""
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
        # 重启后手动查询也要遵守已记录的轮询间隔；系统时钟回拨最多多等一个间隔。
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
        """在进入可取消的轮询前先保存远端任务句柄及登记事件。"""
        await complete_before_cancel(self._register(entry, task, context))

    async def _register(
        self, entry: MCPCapability, task: MCPTask, context: ToolExecutionContext
    ) -> None:
        """给远程任务分配本地引用，保存私有绑定，并在会话日志中登记引用和绑定摘要。"""
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
        # 即使登记句柄或通知观察者时发生取消，收尾代码也需要能找到已收到的任务句柄。
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
        """按远端建议的间隔及本地期限查询原任务，根据观察到的状态决定等待、返回或停止。"""
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
            # 限制最短轮询间隔。等待和后续查询共用同一个截止时间。
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
        """在独立的五秒时限内申请取消并查询结果，将本地取消意图与远端确认区分记录。"""
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
        """获取任务快照，核验返回的任务身份，持久化可公开的状态与已收到的结果信息。"""
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
                # 重试只查询已有任务，不会重复发起工具调用；
                # 原截止时间和对端建议的轮询间隔仍适用。
                await asyncio.sleep(
                    max(0.5 * 2 ** (attempt - 1), (task.poll_interval_ms or 0) / 1000)
                )
            else:
                # 如果记录完成状态时收到取消，应保留已收到的响应，以免对旧状态重复查询或取消。
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
