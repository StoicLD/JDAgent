"""定义运行时依赖的接口，使模型、会话存储、工具和界面可以分别实现或替换。"""

from collections.abc import AsyncIterator, Sequence
from typing import Protocol

from jdagent.domain.events import RuntimeEvent, RuntimeEventType, RuntimePayload
from jdagent.domain.json import JsonObject, JsonValue
from jdagent.domain.mcp import (
    MCPCapabilityKind,
    MCPConnectionState,
    MCPMethod,
    MCPPeerInformation,
    MCPProgressCallback,
)
from jdagent.domain.model import ModelCapabilities, ModelEvent, ModelRequest
from jdagent.domain.tools import (
    ApprovalOutcome,
    ApprovalRequest,
    ToolCall,
    ToolDefinition,
    ToolExecutionContext,
    ToolResult,
)


class ModelPort(Protocol):
    """接受一次模型请求，按到达顺序返回项目定义的模型事件。"""

    @property
    def capabilities(self) -> ModelCapabilities: ...

    def stream(self, request: ModelRequest) -> AsyncIterator[ModelEvent]: ...


class MCPConnectionPort(Protocol):
    """代表一个会话拥有的远端 MCP 连接，协议对象与凭据留在连接适配器内。"""

    @property
    def state(self) -> MCPConnectionState: ...

    @property
    def information(self) -> MCPPeerInformation | None: ...

    async def connect(self) -> MCPPeerInformation: ...

    def input_schema_issue(self, schema: JsonObject) -> str | None:
        """在公开工具之前，检查输入定义是否符合已协商的协议限制。"""
        ...

    def capability_issue(self, kind: MCPCapabilityKind, definition: JsonValue) -> str | None:
        """单独校验一项目录条目，使同批中有效的能力仍可使用。"""
        ...

    async def request(
        self,
        method: MCPMethod,
        params: JsonObject,
        *,
        on_progress: MCPProgressCallback | None = None,
        input_schema: JsonObject | None = None,
    ) -> JsonObject: ...

    async def aclose(self) -> bool: ...


class SessionPort(Protocol):
    """按会话追加和读取持久事件，供执行时记录以及重启后恢复。"""

    async def append(self, event: RuntimeEvent) -> None: ...

    def read(self, session_id: str) -> AsyncIterator[RuntimeEvent]: ...

    async def acquire(self, session_id: str) -> None: ...

    async def release(self, session_id: str) -> None: ...


class ContextObjects(Protocol):
    """按内容摘要保存和校验不可变对象。事件只有在对象写入后才能引用其摘要。"""

    async def put(self, content: bytes) -> str: ...

    async def get(self, digest: str) -> bytes: ...


class SessionDiscoveryPort(Protocol):
    """枚举可供选择的会话标识，使会话目录无需了解底层文件布局。"""

    async def list_session_ids(self) -> Sequence[str]: ...


class ToolRuntimePort(Protocol):
    """接收参数已收齐的工具调用，负责校验、权限判定、实际执行和结果返回。"""

    async def execute(self, call: ToolCall, context: ToolExecutionContext) -> ToolResult: ...


class DynamicToolsPort(Protocol):
    """在模型请求之间调整可见工具；实际执行仍按调用时生效的授权检查。"""

    async def prepare_tools(self) -> tuple[ToolDefinition, ...]: ...

    def allowed(self, definition: ToolDefinition) -> bool: ...

    def used(self, definition: ToolDefinition) -> None: ...

    def unload(self, definition: ToolDefinition) -> None: ...


class RequestToolsPort(Protocol):
    """固定某次模型请求实际包含的工具定义，供后续执行核对。"""

    async def prepare_request(self) -> tuple[ToolDefinition, ...]: ...

    def reduce_tools(self, request: ModelRequest) -> ModelRequest: ...

    def bind_request(self, request: ModelRequest) -> ModelRequest: ...


class ApprovalPort(Protocol):
    """在策略要求询问时收集用户的审批决定。"""

    async def request(self, request: ApprovalRequest) -> ApprovalOutcome: ...


class RuntimeEventSink(Protocol):
    """接收完整事件，先持久化，再通知观察者。"""

    async def emit(self, event: RuntimeEvent) -> None: ...


class EventObserver(Protocol):
    """消费已经写入的运行事件，供界面或追踪使用。"""

    async def observe(self, event: RuntimeEvent) -> None: ...


class RuntimeEventRecorder(Protocol):
    """为当前会话创建并持久化下一个事件。"""

    async def record(
        self,
        turn_id: str | None,
        event_type: RuntimeEventType,
        payload: RuntimePayload,
    ) -> RuntimeEvent: ...


class RuntimeJournal(RuntimeEventRecorder, Protocol):
    """为执行器提供会话历史的只读视图和新事件的写入接口。"""

    @property
    def events(self) -> Sequence[RuntimeEvent]: ...
