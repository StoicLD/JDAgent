"""One lazy, sequential SDK session owned and closed by the same asyncio task."""

import asyncio
import os
from contextlib import AsyncExitStack
from dataclasses import dataclass
from typing import cast

import httpx2
from mcp import MCPError, types
from mcp.client import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.shared.jsonrpc_dispatcher import JSONRPCDispatcher
from mcp.shared.transport_context import TransportContext
from pydantic import ValidationError

from jdagent.adapters.mcp_stdio import bounded_stdio_client
from jdagent.adapters.mcp_tasks import TASKS_EXTENSION, TasksExtension
from jdagent.adapters.mcp_transport import (
    BoundedHttpTransport,
    ResponseEncodingError,
    ResponseLimitExceeded,
)
from jdagent.domain.json import JsonObject, normalize_json, require_object
from jdagent.domain.mcp import (
    MCPConnectionState,
    MCPFailure,
    MCPMethod,
    MCPPeerInformation,
)
from jdagent.mcp_configuration import MCPServerConfiguration


@dataclass(slots=True)
class _Request:
    method: MCPMethod | None
    params: JsonObject
    result: asyncio.Future[JsonObject | MCPFailure]


class SDKMCPConnection:
    """Reuse a peer across caller tasks, without sharing it across host Sessions.

    Only one request may be active. The owner task holds the SDK contexts, not a
    business workflow. Failed/closed connections require explicit replacement.
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

    async def request(self, method: MCPMethod, params: JsonObject) -> JsonObject:
        """Return an SDK-validated result or a safe failure; never retry a send."""
        return await self._request(method, params)

    async def connect(self) -> MCPPeerInformation:
        """Negotiate capabilities without assuming the peer offers Tools."""
        await self._request(None, {})
        assert self._information is not None
        return self._information

    async def _request(self, method: MCPMethod | None, params: JsonObject) -> JsonObject:
        if self._state in ("unavailable", "closed") or self._closing:
            raise MCPFailure("connection")
        if self._pending is not None:
            raise MCPFailure("busy")
        request = _Request(method, params, asyncio.get_running_loop().create_future())
        self._pending = request
        self._queue.put_nowait(request)
        if self._owner is None:
            self._state = "connecting"
            self._owner = asyncio.create_task(self._run(), name="mcp-connection-owner")
        try:
            result = await asyncio.shield(request.result)
            if isinstance(result, MCPFailure):
                raise result
            return result
        except asyncio.CancelledError:
            await self.aclose()
            raise
        finally:
            self._pending = None

    async def aclose(self) -> bool:
        """Wait at most five seconds; retain ownership if cleanup is unfinished."""
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
        failure = MCPFailure("connection")
        try:
            async with AsyncExitStack() as stack:
                async with asyncio.timeout(15):
                    session = await self._connect(stack)
                self._state = "ready"
                while True:
                    request = await self._queue.get()
                    try:
                        result = await self._send(session, request)
                    except MCPError as error:
                        if error.code == types.CONNECTION_CLOSED:
                            raise
                        result = MCPFailure("remote", rpc_code=error.code)
                    if not request.result.done():
                        request.result.set_result(result)
        except asyncio.CancelledError:
            pass
        except Exception as error:
            # SDK transports may raise ExceptionGroup during context teardown.
            # Do not expose nested exception messages, URLs, or server content.
            failure = _safe_failure(error)
        finally:
            failure = self._stream_error or failure
            self._state = "closed" if self._closing else "unavailable"
            if self._pending is not None and not self._pending.result.done():
                self._pending.result.set_result(failure)

    async def _connect(self, stack: AsyncExitStack) -> ClientSession:
        config = self._configuration
        if not config.enabled:
            raise MCPFailure("configuration")
        if config.transport == "stdio":
            if config.command is None or config.cwd is None:
                raise MCPFailure("configuration")
            environment = {
                name: self._credential(reference) for name, reference in config.environment_refs
            }
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
        dispatcher: JSONRPCDispatcher[TransportContext] = JSONRPCDispatcher(
            read, write, on_stream_exception=self._stream_failure
        )
        session = await stack.enter_async_context(
            ClientSession(
                dispatcher=dispatcher,
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
        # The SDK's SSE path translates read failures to CONNECTION_CLOSED.
        # Retain the bounded transport's category before that information is lost.
        if self._stream_error is None:
            self._stream_error = _safe_failure(error)

    async def _negotiate(
        self, dispatcher: JSONRPCDispatcher[TransportContext], session: ClientSession
    ) -> None:
        # send_discover() has its own 10-second deadline. The public dispatcher
        # lets the host apply one 15-second connection budget, including fallback.
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
        # Avoid ClientSession.list_tools(): its unbounded schema cache would be
        # a third cache outside the host's directory/loaded-definition budgets.
        if request.method is None:
            return {}
        wire = {"method": request.method, "params": request.params}
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
        return require_object(
            normalize_json(result.model_dump(by_alias=True, mode="json", exclude_none=True)),
            "MCP result",
        )


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
