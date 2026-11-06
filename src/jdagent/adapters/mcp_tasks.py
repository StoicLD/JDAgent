"""Modern Tasks wire types and explicit SDK operations, confined to the adapter."""

from collections.abc import Sequence
from typing import Literal, Self

from mcp import types
from mcp.client import ClaimContext, ClientExtension, ClientSession, ResultClaim
from pydantic import Field, model_validator

TASKS_EXTENSION = "io.modelcontextprotocol/tasks"
TaskStatus = Literal["working", "input_required", "completed", "failed", "cancelled"]


class _TaskFields(types.Result):
    task_id: str = Field(min_length=1)
    status: TaskStatus
    created_at: str
    last_updated_at: str
    ttl_ms: int | None = Field(ge=0, strict=True)
    poll_interval_ms: int | None = Field(default=None, ge=0, strict=True)
    status_message: str | None = None


class TaskHandle(_TaskFields):
    """Creation response; the host must persist this before its first poll."""

    result_type: Literal["task"] = "task"


class TaskState(_TaskFields):
    """Polling response, retaining terminal business errors separately from status."""

    result_type: Literal["complete"] = "complete"
    result: types.CallToolResult | None = None
    error: types.ErrorData | None = None
    input_requests: types.InputRequests | None = None

    @model_validator(mode="after")
    def validate_status_payload(self) -> Self:
        if self.status == "completed" and self.result is None:
            raise ValueError("Completed task has no result")
        if self.status == "failed" and self.error is None:
            raise ValueError("Failed task has no error")
        if self.status == "input_required" and not self.input_requests:
            raise ValueError("Task requires input but has no requests")
        return self


async def _require_host_polling(handle: TaskHandle, context: ClaimContext) -> types.CallToolResult:
    raise RuntimeError("Use the session API and persist the Task handle before polling")


class TasksExtension(ClientExtension):
    """Claim modern task handles without starting an implicit polling loop."""

    identifier = TASKS_EXTENSION

    def claims(self) -> Sequence[ResultClaim[TaskHandle]]:
        return (
            ResultClaim(
                result_type="task",
                model=TaskHandle,
                resolve=_require_host_polling,
                protocol_versions=frozenset({"2026-07-28"}),
            ),
        )


class _TaskParams(types.RequestParams):
    task_id: str = Field(min_length=1)


class _GetTask(types.Request[_TaskParams, Literal["tasks/get"]]):
    method: Literal["tasks/get"] = "tasks/get"
    name_param = "taskId"


class _CancelTask(types.Request[_TaskParams, Literal["tasks/cancel"]]):
    method: Literal["tasks/cancel"] = "tasks/cancel"
    name_param = "taskId"


def _check_support(session: ClientSession) -> None:
    capabilities = session.server_capabilities
    if (
        session.protocol_version != "2026-07-28"
        or capabilities is None
        or TASKS_EXTENSION not in (capabilities.extensions or {})
    ):
        raise ValueError("Server does not support modern Tasks")


async def get_task(
    session: ClientSession, task_id: str, *, timeout_seconds: float | None = None
) -> TaskState:
    """Fetch only the existing task; do not repeat its originating operation."""
    _check_support(session)
    state = await session.send_request(
        _GetTask(params=_TaskParams(task_id=task_id)),
        TaskState,
        request_read_timeout_seconds=timeout_seconds,
    )
    if state.task_id != task_id:
        raise ValueError("Server returned a different Task identity")
    return state


async def cancel_task(
    session: ClientSession, task_id: str, *, timeout_seconds: float | None = None
) -> None:
    """Request cancellation; the acknowledgement does not prove work stopped."""
    _check_support(session)
    await session.send_request(
        _CancelTask(params=_TaskParams(task_id=task_id)),
        types.EmptyResult,
        request_read_timeout_seconds=timeout_seconds,
    )
