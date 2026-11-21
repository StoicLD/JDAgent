"""在容量限制内保存已加载的 MCP 定义，按其身份追踪待处理操作。"""

import hashlib
import json
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from jdagent.domain.mcp import MCPCapability


@dataclass(frozen=True, slots=True)
class MCPDefinitionLimits:
    """限制可载入工具定义的数量、大小等资源，避免完整服务目录占满模型请求。"""

    count: int = 16
    tokens: int = 8192
    pending_bytes: int = 8192

    def __post_init__(self) -> None:
        if min(self.count, self.tokens, self.pending_bytes) <= 0:
            raise ValueError("MCP definition limits must be positive")


@dataclass(frozen=True, slots=True)
class MCPSelection:
    """暂存被选中定义的身份、目录版本、摘要与用量估计，供下一次请求重新校验。"""

    server_id: str
    name: str
    generation: str
    digest: str
    tokens: int

    @property
    def key(self) -> tuple[str, str]:
        return self.server_id, self.name


def definition_digest(entry: MCPCapability) -> str:
    return hashlib.sha256(entry.definition_json.encode("utf-8")).hexdigest()


def model_tool_name(server_id: str, name: str) -> str:
    """将服务标识与工具原名编码为可供模型调用的名称；超过长度上限时用摘要缩短，实际路由由宿主保存。"""

    def escaped(text: str) -> str:
        return "".join(
            chr(value)
            if 48 <= value <= 57 or 65 <= value <= 90 or 97 <= value <= 122
            else f"_{value:02x}"
            for value in text.encode("utf-8")
        )

    encoded = f"mcp__{escaped(server_id)}__{escaped(name)}"
    if len(encoded) <= 64:
        return encoded
    digest = hashlib.sha256(
        json.dumps([server_id, name], ensure_ascii=False).encode("utf-8")
    ).hexdigest()
    return "mcp__" + digest[:48]


def definition_tokens(entry: MCPCapability) -> int:
    # 沿用项目尚未校准时的保守估算，用字节数估计定义占用的输入预算。
    return (
        len(entry.definition_json.encode("utf-8"))
        + len(model_tool_name(entry.server_id, entry.name))
        + 128
    )


class MCPDefinitionCache:
    """管理待加载和已加载的工具定义。完整定义只在两次模型请求之间切换。"""

    def __init__(self, limits: MCPDefinitionLimits) -> None:
        self._limits = limits
        self._loaded: OrderedDict[tuple[str, str], MCPCapability] = OrderedDict()
        self._pending: OrderedDict[tuple[str, str], MCPSelection] = OrderedDict()

    def stage(self, entry: MCPCapability) -> str:
        """检查待选定义的预算，保留足以在下次准备请求时识别同一版本的信息。"""
        selection = MCPSelection(
            entry.server_id,
            entry.name,
            entry.generation,
            definition_digest(entry),
            definition_tokens(entry),
        )
        candidates = {**self._pending, selection.key: selection}
        if selection.tokens > self._limits.tokens:
            return "definition_too_large"
        if (
            len(candidates) > self._limits.count
            or sum(item.tokens for item in candidates.values()) > self._limits.tokens
        ):
            return "selection_capacity"
        size = sum(
            len((item.server_id + item.name + item.generation + item.digest).encode("utf-8")) + 64
            for item in candidates.values()
        )
        if size > self._limits.pending_bytes:
            return "selection_capacity"
        self._pending[selection.key] = selection
        return "pending_next_request"

    async def prepare(
        self, resolve: Callable[[MCPSelection], Awaitable[MCPCapability | None]]
    ) -> tuple[MCPCapability, ...]:
        """核对待选工具与当前目录中的版本和摘要，将仍有效的定义装入下一次请求。"""
        pending, self._pending = self._pending, OrderedDict()
        protected = set(pending)
        for selection in pending.values():
            entry = await resolve(selection)
            if entry is None:
                continue
            self._loaded[selection.key] = entry
            self._loaded.move_to_end(selection.key)
            while (
                len(self._loaded) > self._limits.count
                or sum(definition_tokens(item) for item in self._loaded.values())
                > self._limits.tokens
            ):
                victim = next(key for key in self._loaded if key not in protected)
                del self._loaded[victim]
        return tuple(self._loaded.values())

    def invalidate(self, server_id: str) -> None:
        for key in tuple(self._loaded):
            if key[0] == server_id:
                del self._loaded[key]
        for key in tuple(self._pending):
            if key[0] == server_id:
                del self._pending[key]

    def used(self, server_id: str, name: str) -> None:
        if (server_id, name) in self._loaded:
            self._loaded.move_to_end((server_id, name))

    def unload(self, server_id: str, name: str) -> None:
        """从后续请求中释放已加载的定义，原条目仍可在能力目录中检索。"""
        self._loaded.pop((server_id, name), None)
