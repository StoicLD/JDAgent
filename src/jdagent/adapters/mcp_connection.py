"""由同一个异步任务建立、使用和关闭 MCP 会话，依次执行协议请求。"""

import asyncio
import hashlib
import json
import math
import os
from collections.abc import Mapping
from contextlib import AsyncExitStack
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any, TypeAlias, cast

import httpx2
from mcp import MCPError, types
from mcp.client import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.shared.dispatcher import CallOptions
from mcp.shared.inbound import find_invalid_x_mcp_header, mcp_param_headers, x_mcp_header_map
from mcp.shared.jsonrpc_dispatcher import JSONRPCDispatcher
from mcp.shared.transport_context import TransportContext
from mcp_types import RequestId
from pydantic import TypeAdapter, ValidationError

from jdagent.adapters.mcp_stdio import bounded_stdio_client
from jdagent.adapters.mcp_tasks import (
    TASKS_EXTENSION,
    TaskHandle,
    TasksExtension,
    cancel_task,
    get_task,
)
from jdagent.adapters.mcp_transport import (
    BoundedHttpTransport,
    ResponseEncodingError,
    ResponseLimitExceeded,
)
from jdagent.domain.json import (
    JsonObject,
    JsonValue,
    normalize_json,
    require_object,
    require_string,
)
from jdagent.domain.mcp import (
    MCPCapabilityKind,
    MCPConnectionState,
    MCPFailure,
    MCPMethod,
    MCPPeerInformation,
    MCPProgressCallback,
)
from jdagent.mcp_configuration import MCPServerConfiguration

_TaskCallResult: TypeAlias = types.CallToolResult | TaskHandle
# Pydantic 支持这里的联合类型，当前锁定版本的 Pyright 无法识别这个 TypeForm 参数。
_TASK_CALL_RESULT = TypeAdapter[_TaskCallResult](_TaskCallResult)  # pyright: ignore[reportArgumentType]
_PARAMETER_HEADERS: ContextVar[dict[str, str] | None] = ContextVar(
    "mcp_parameter_headers", default=None
)
_DIRECTORY_KEYS = {
    "tools/list": "tools",
    "resources/list": "resources",
    "resources/templates/list": "resourceTemplates",
    "prompts/list": "prompts",
}
_DIRECTORY_ENTRIES: ContextVar[JsonObject | None] = ContextVar(
    "mcp_directory_entries", default=None
)


class _ParameterHeaderDispatcher(JSONRPCDispatcher[TransportContext]):
    """将本次调用需要的参数请求头交给 SDK，由 SDK 校验并发送请求。

    SDK 2.3 的常规接口忽略 metadata.headers，默认解析方式依赖无容量限制的工具缓存。
    这里根据宿主为本次请求保留的工具定义生成请求头。
    """

    async def send_raw_request(
        self,
        method: str,
        params: Mapping[str, Any] | None,
        opts: CallOptions | None = None,
        *,
        _related_request_id: RequestId | None = None,
    ) -> dict[str, Any]:
        options: CallOptions = {**(opts or {})}
        if method == "tools/call" and (headers := _PARAMETER_HEADERS.get()):
            options["headers"] = {**options.get("headers", {}), **headers}
        raw = await super().send_raw_request(
            method, params, options, _related_request_id=_related_request_id
        )
        entries = _DIRECTORY_ENTRIES.get()
        key = _DIRECTORY_KEYS.get(method)
        if entries is not None and key is not None and isinstance(raw.get(key), list):
            # SDK 默认整批校验列表。先用空列表校验响应外层，再逐项校验原始条目，
            # 目录才能指出哪一项无效。
            entries[key] = normalize_json(raw[key])
            return {**raw, key: []}
        return raw


@dataclass(slots=True)
class _Request:
    """表示排入连接队列的一次调用，关联等待者、发送任务和完成结果。"""

    method: MCPMethod | None
    params: JsonObject
    result: asyncio.Future[JsonObject | MCPFailure]
    on_progress: MCPProgressCallback | None = None
    input_schema: JsonObject | None = None
    operation: asyncio.Task[JsonObject] | None = None
    abandoned: bool = False


class SDKMCPConnection:
    """管理单个宿主会话的 MCP 连接，一次只允许一个请求使用连接。

    同一异步任务负责建立、收发和关闭连接；失败或关闭后需要显式替换连接。
    """

    def __init__(self, configuration: MCPServerConfiguration) -> None:
        self._configuration = configuration
        self._state: MCPConnectionState = "not_connected"
        self._information: MCPPeerInformation | None = None
        self._owner: asyncio.Task[None] | None = None
        self._queue: asyncio.Queue[_Request] = asyncio.Queue(maxsize=1)
        self._pending: _Request | None = None
        self._closing = False
        self._stream_error: MCPFailure | None = None

    @property
    def state(self) -> MCPConnectionState:
        return self._state

    @property
    def information(self) -> MCPPeerInformation | None:
        return self._information

    def input_schema_issue(self, schema: JsonObject) -> str | None:
        if (
            self._information is not None
            and self._information.protocol_version == "2026-07-28"
            and find_invalid_x_mcp_header(schema) is not None
        ):
            return "invalid_header_annotation"
        return None

    def capability_issue(self, kind: MCPCapabilityKind, definition: JsonValue) -> str | None:
        model = {
            "tool": types.Tool,
            "resource": types.Resource,
            "resource_template": types.ResourceTemplate,
            "prompt": types.Prompt,
        }[kind]
        try:
            model.model_validate(definition)
        except ValidationError:
            return "invalid_definition"
        return None

    async def request(
        self,
        method: MCPMethod,
        params: JsonObject,
        *,
        on_progress: MCPProgressCallback | None = None,
        input_schema: JsonObject | None = None,
    ) -> JsonObject:
        """校验并转发一次协议请求。目录中的具体条目在对模型公开之前还须逐项校验。"""
        return await self._request(
            method, params, on_progress=on_progress, input_schema=input_schema
        )

    async def connect(self) -> MCPPeerInformation:
        """建立连接并协商能力，只有远端实际声明的能力才可在后续使用。"""
        await self._request(None, {})
        assert self._information is not None
        return self._information

    async def _request(
        self,
        method: MCPMethod | None,
        params: JsonObject,
        *,
        on_progress: MCPProgressCallback | None = None,
        input_schema: JsonObject | None = None,
    ) -> JsonObject:
        """将请求交给拥有连接的异步任务并等待结果；已有请求占用连接时报告繁忙。"""
        if self._state in ("unavailable", "closed") or self._closing:
            raise MCPFailure("connection")
        if self._pending is not None:
            raise MCPFailure("busy")
        request = _Request(
            method, params, asyncio.get_running_loop().create_future(), on_progress, input_schema
        )
        self._pending = request
        if self._owner is None:
            self._state = "connecting"
            self._owner = asyncio.create_task(self._run(), name="mcp-connection-owner")
        try:
            await self._queue.put(request)
            result = await asyncio.shield(request.result)
            if isinstance(result, MCPFailure):
                raise result
            return result
        except asyncio.CancelledError:
            request.abandoned = True
            if method in {"tasks/get", "tasks/cancel"}:
                # 取消只结束本次读取。连接所有者仍须收尾这次请求，并保留连接以发送任务取消操作。
                if request.operation is not None:
                    request.operation.cancel()
            else:
                await self.aclose()
            raise
        finally:
            self._pending = None

    async def aclose(self) -> bool:
        """请求连接所有者关闭资源，最多等待五秒。清理未结束时保留所有权并报告尚未关闭。"""
        self._closing = True
        if self._owner is None:
            self._state = "closed"
            return True
        if not self._owner.done() and self._owner.cancelling() == 0:
            self._owner.cancel()
        done, _ = await asyncio.wait({self._owner}, timeout=5)
        if self._pending is not None and not self._pending.result.done():
            self._pending.result.set_result(MCPFailure("connection"))
        if not done:
            self._state = "unavailable"
            return False
        self._state = "closed"
        return True

    async def _run(self) -> None:
        """由连接所有者串行消费请求队列，将每次结果或故障传回相应等待者。"""
        failure = MCPFailure("connection")
        try:
            async with AsyncExitStack() as stack:
                async with asyncio.timeout(15):
                    session = await self._connect(stack)
                self._state = "ready"
                while True:
                    request = await self._queue.get()
                    if request.abandoned:
                        continue
                    request.operation = asyncio.create_task(
                        self._send(session, request), name="mcp-owned-rpc"
                    )
                    try:
                        result = await request.operation
                    except asyncio.CancelledError:
                        if self._closing or self._owner is not None and self._owner.cancelling():
                            raise
                        # 子查询取消后要等它收尾，才能在同一连接上发起下一次请求。
                        result = MCPFailure("connection")
                    except MCPError as error:
                        if error.code == types.CONNECTION_CLOSED:
                            raise
                        # 当前会话没有设置 SDK 请求计时器。这里的 REQUEST_TIMEOUT 来自对端；
                        # 宿主超时走取消路径。
                        kind = (
                            "server_timeout"
                            if request.method in {"tools/call", "tasks/get"}
                            and error.code == types.REQUEST_TIMEOUT
                            else "task_unavailable"
                            if request.method in {"tasks/get", "tasks/cancel"}
                            and error.code == types.INVALID_PARAMS
                            else "remote"
                        )
                        result = MCPFailure(kind, rpc_code=error.code)
                    if not request.result.done():
                        request.result.set_result(result)
        except asyncio.CancelledError:
            pass
        except Exception as error:
            # SDK 在关闭传输时可能抛出异常组，其中的详细信息可能含有地址或服务器原文。
            failure = _safe_failure(error)
        finally:
            failure = self._stream_error or failure
            self._state = "closed" if self._closing else "unavailable"
            if self._pending is not None and not self._pending.result.done():
                self._pending.result.set_result(failure)

    async def _connect(self, stack: AsyncExitStack) -> ClientSession:
        """根据配置建立标准流或 HTTP 连接，并以实际使用的连接参数建立身份校验依据。"""
        config = self._configuration
        if not config.enabled:
            raise MCPFailure("configuration")
        # 绑定摘要使用端点和连接实际采用的配置值，以便恢复时核对身份。它只保存在私有控制数据中。
        identity: dict[str, object] = {
            "transport": config.transport,
            "command": config.command,
            "args": config.args,
            "cwd": str(config.cwd) if config.cwd is not None else None,
            "url": config.url,
        }
        if config.transport == "stdio":
            if config.command is None or config.cwd is None:
                raise MCPFailure("configuration")
            environment = {
                name: self._credential(reference) for name, reference in config.environment_refs
            }
            identity["environment"] = environment
            read, write = await stack.enter_async_context(
                bounded_stdio_client(
                    config.command, config.args, cwd=config.cwd, environment=environment
                )
            )
        else:
            if config.url is None:
                raise MCPFailure("configuration")
            headers: dict[str, str] = {}
            if config.bearer_env is not None:
                headers["Authorization"] = "Bearer " + self._credential(config.bearer_env)
            identity["headers"] = headers
            http = await stack.enter_async_context(
                httpx2.AsyncClient(
                    transport=BoundedHttpTransport(
                        httpx2.AsyncHTTPTransport(retries=0),
                        on_failure=self._remember_transport_failure,
                    ),
                    timeout=httpx2.Timeout(15, read=None),
                    trust_env=False,
                    headers=headers,
                )
            )
            read, write = await stack.enter_async_context(
                streamable_http_client(config.url, http_client=http)
            )
        dispatcher: JSONRPCDispatcher[TransportContext] = _ParameterHeaderDispatcher(
            read, write, on_stream_exception=self._stream_failure
        )
        session = await stack.enter_async_context(
            ClientSession(
                dispatcher=dispatcher,
                read_timeout_seconds=None,
                client_info=types.Implementation(name="JDAgent", version="0.7"),
                extensions={TASKS_EXTENSION: {}},
                result_claims={TASKS_EXTENSION: TasksExtension().claims()},
            )
        )
        await self._negotiate(dispatcher, session)
        capabilities = session.server_capabilities
        assert capabilities is not None and session.protocol_version is not None
        self._information = MCPPeerInformation(
            protocol_version=session.protocol_version,
            tools=capabilities.tools is not None,
            resources=capabilities.resources is not None,
            prompts=capabilities.prompts is not None,
            tasks=session.protocol_version == "2026-07-28"
            and TASKS_EXTENSION in (capabilities.extensions or {}),
            identity_digest=hashlib.sha256(
                json.dumps(identity, sort_keys=True, separators=(",", ":")).encode("utf-8")
            ).hexdigest(),
        )
        return session

    @staticmethod
    def _credential(reference: str) -> str:
        value = os.environ.get(reference)
        if not value:
            raise MCPFailure("configuration")
        return value

    async def _stream_failure(self, error: Exception) -> None:
        self._remember_transport_failure(error)
        if self._owner is not None:
            self._owner.cancel()

    def _remember_transport_failure(self, error: Exception) -> None:
        # SDK 会把 SSE 读取错误统一报为连接关闭，须先取回传输层记录的具体错误类别。
        if self._stream_error is None:
            self._stream_error = _safe_failure(error)

    async def _negotiate(
        self, dispatcher: JSONRPCDispatcher[TransportContext], session: ClientSession
    ) -> None:
        """与对端协商协议版本及能力，在同一连接预算内处理发现请求和回退。"""
        # 直接调用发现接口会单独计时十秒。这里通过公共分发接口让发现和回退共用十五秒连接预算。
        try:
            raw = await dispatcher.send_raw_request(
                "server/discover",
                {
                    "_meta": {
                        "io.modelcontextprotocol/protocolVersion": "2026-07-28",
                        "io.modelcontextprotocol/clientInfo": {"name": "JDAgent", "version": "0.7"},
                        "io.modelcontextprotocol/clientCapabilities": {
                            "extensions": {TASKS_EXTENSION: {}}
                        },
                    }
                },
                {
                    "cancel_on_abandon": False,
                    "headers": {
                        "MCP-Protocol-Version": "2026-07-28",
                        "Mcp-Method": "server/discover",
                    },
                },
            )
        except MCPError as error:
            if error.code != types.METHOD_NOT_FOUND:
                raise
            result = await session.initialize()
            if result.protocol_version != "2025-11-25":
                raise MCPFailure("unsupported") from None
        else:
            discovery = types.DiscoverResult.model_validate(raw)
            if "2026-07-28" not in discovery.supported_versions:
                raise MCPFailure("unsupported")
            session.adopt(discovery)

    async def _send(self, session: ClientSession, request: _Request) -> JsonObject:
        """按协商的方法分派一次请求，处理进度和结果，并将 SDK 层返回值转换为项目数据。"""
        # SDK 的 list_tools 会额外缓存完整定义。由宿主管理目录和已加载定义，才能约束缓存大小。
        if request.method is None:
            return {}

        async def report_progress(
            progress: float, total: float | None, message: str | None
        ) -> None:
            # 进度通知只采纳有限数值；对端附带的文字不写入日志，通知也不延长超时或触发重试。
            del message
            if isinstance(progress, bool) or not math.isfinite(progress) or progress < 0:
                return
            if total is not None and (
                isinstance(total, bool) or not math.isfinite(total) or total < progress
            ):
                return
            if not request.abandoned and request.on_progress is not None:
                await request.on_progress(progress, total)

        progress_callback = report_progress if request.on_progress is not None else None
        wire = {"method": request.method, "params": request.params}
        match request.method:
            case "resources/read":
                result = await session.send_request(
                    types.ReadResourceRequest.model_validate(wire),
                    types.ReadResourceResult,
                    progress_callback=progress_callback,
                )
            case "prompts/get":
                result = await session.send_request(
                    types.GetPromptRequest.model_validate(wire),
                    types.GetPromptResult,
                    progress_callback=progress_callback,
                )
            case "tools/call":
                headers: dict[str, str] = {}
                if (
                    request.input_schema is not None
                    and self._information is not None
                    and self._information.protocol_version == "2026-07-28"
                ):
                    if self.input_schema_issue(request.input_schema) is not None:
                        raise MCPFailure("unsupported")
                    headers = mcp_param_headers(
                        x_mcp_header_map(request.input_schema),
                        require_object(request.params.get("arguments", {}), "MCP arguments"),
                    )
                token = _PARAMETER_HEADERS.set(headers)
                try:
                    result = await session.send_request(
                        types.CallToolRequest.model_validate(wire),
                        _TASK_CALL_RESULT
                        if self._information is not None and self._information.tasks
                        else types.CallToolResult,
                        progress_callback=progress_callback,
                    )
                finally:
                    _PARAMETER_HEADERS.reset(token)
            case "tasks/get" | "tasks/cancel":
                if self._information is None or not self._information.tasks:
                    raise MCPFailure("unsupported")
                task_id = require_string(request.params, "taskId")
                if request.method == "tasks/cancel":
                    await cancel_task(session, task_id, progress_callback=progress_callback)
                    return {}
                result = await get_task(session, task_id, progress_callback=progress_callback)
            case "tools/list" | "resources/list" | "resources/templates/list" | "prompts/list":
                return await self._list(session, request)
        return require_object(
            normalize_json(result.model_dump(by_alias=True, mode="json", exclude_unset=True)),
            "MCP result",
        )

    async def _list(self, session: ClientSession, request: _Request) -> JsonObject:
        entries: JsonObject = {}
        token = _DIRECTORY_ENTRIES.set(entries)
        wire = {"method": request.method, "params": request.params}
        try:
            match request.method:
                case "tools/list":
                    result = await session.send_request(
                        types.ListToolsRequest.model_validate(wire), types.ListToolsResult
                    )
                case "resources/list":
                    result = await session.send_request(
                        types.ListResourcesRequest.model_validate(wire), types.ListResourcesResult
                    )
                case "resources/templates/list":
                    result = await session.send_request(
                        types.ListResourceTemplatesRequest.model_validate(wire),
                        types.ListResourceTemplatesResult,
                    )
                case "prompts/list":
                    result = await session.send_request(
                        types.ListPromptsRequest.model_validate(wire), types.ListPromptsResult
                    )
                case _:
                    raise MCPFailure("unsupported")
            normalized = require_object(
                normalize_json(result.model_dump(by_alias=True, mode="json", exclude_unset=True)),
                "MCP directory result",
            )
            return {**normalized, **entries}
        finally:
            _DIRECTORY_ENTRIES.reset(token)


def _safe_failure(error: BaseException) -> MCPFailure:
    if isinstance(error, MCPFailure):
        return MCPFailure(error.kind, rpc_code=error.rpc_code)
    if isinstance(error, TimeoutError):
        return MCPFailure("connection_timeout")
    if isinstance(error, ResponseLimitExceeded):
        return MCPFailure("response_limit")
    if isinstance(error, (ValidationError, ResponseEncodingError)):
        return MCPFailure("protocol")
    if isinstance(error, BaseExceptionGroup):
        for nested in cast(BaseExceptionGroup[BaseException], error).exceptions:
            failure = _safe_failure(nested)
            if failure.kind != "connection":
                return failure
    return MCPFailure("connection")
