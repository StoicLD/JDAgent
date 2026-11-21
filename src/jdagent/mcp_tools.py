"""向模型提供 MCP 能力发现工具，并将已加载的工具绑定到调用目标。"""

import json
from dataclasses import asdict

from jdagent.domain.json import JsonObject, normalize_json, require_object, require_string
from jdagent.domain.mcp import MCPCapability
from jdagent.domain.tools import (
    RiskLevel,
    ToolDefinition,
    ToolErrorCode,
    ToolExecutionContext,
    ToolOrigin,
    ToolOutcome,
)
from jdagent.mcp_definitions import definition_digest, model_tool_name
from jdagent.mcp_execution import MCPExecution
from jdagent.mcp_material_tools import MCPMaterialTools
from jdagent.mcp_service import MCPService


class MCPTools:
    """将本轮的模型工具与会话级能力缓存连接。搜索目录只改变候选定义，业务执行另行发起。"""

    def __init__(self, service: MCPService, session_id: str, execution: MCPExecution) -> None:
        self._service = service
        self._session_id = session_id
        self._execution = execution

    async def prepare_tools(self) -> tuple[ToolDefinition, ...]:
        loaded = await self._service.loaded_tools(self._session_id)
        return (
            self._search_definition(),
            self._read_definition(),
            *MCPMaterialTools(self._service, self._execution, self._session_id).definitions(),
            *(self._definition(entry) for entry in loaded),
        )

    def _read_definition(self) -> ToolDefinition:
        return ToolDefinition(
            "read_mcp_result",
            "Read a saved MCP original as a bounded JSON text page. Use the original sha256 "
            "from a previous MCP result in this Session; offset and limit count "
            "Unicode characters.",
            {
                "type": "object",
                "properties": {
                    "sha256": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
                    "offset": {"type": "integer", "minimum": 0},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 2048},
                },
                "required": ["sha256"],
                "additionalProperties": False,
            },
            RiskLevel.READ,
            self._execution.read_result,
            manages_timeout=True,
            origin=ToolOrigin("mcp_control"),
        )

    def allowed(self, definition: ToolDefinition) -> bool:
        origin = definition.origin
        if origin.kind == "mcp_control":
            return True
        return self._service.tool_allowed(
            self._session_id, origin.server_id or "", origin.raw_name or "", origin.generation or ""
        )

    def used(self, definition: ToolDefinition) -> None:
        origin = definition.origin
        if origin.server_id is not None and origin.raw_name is not None:
            self._service.tool_used(self._session_id, origin.server_id, origin.raw_name)

    def unload(self, definition: ToolDefinition) -> None:
        origin = definition.origin
        if origin.server_id is not None and origin.raw_name is not None:
            self._service.unload_tool(self._session_id, origin.server_id, origin.raw_name)

    def _search_definition(self) -> ToolDefinition:
        return ToolDefinition(
            "search_tools",
            "Find allowed MCP tools by keywords (BM25). Selected tools become available on the "
            "next model request if the complete definition fits its input budget. "
            "Use Server filters or rewrite keywords when coverage is "
            "incomplete or there are no matches.",
            {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "minLength": 1, "maxLength": 512},
                    "server_id": {"type": "string"},
                    "offset": {"type": "integer", "minimum": 0},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 20},
                },
                "required": ["query"],
                "additionalProperties": False,
            },
            RiskLevel.READ,
            self._search,
            manages_timeout=True,
            origin=ToolOrigin("mcp_control"),
        )

    async def _search(self, arguments: JsonObject, context: ToolExecutionContext) -> ToolOutcome:
        """在会话目录中查找匹配工具，登记可在下一次模型请求中加载的定义。"""
        if context.session_id != self._session_id:
            return ToolOutcome(completed=False, error_code=ToolErrorCode.TOOL_UNAVAILABLE)
        query = require_string(arguments, "query")
        server = arguments.get("server_id")
        offset, limit = arguments.get("offset", 0), arguments.get("limit", 10)
        if (
            (server is not None and not isinstance(server, str))
            or not isinstance(offset, int)
            or not isinstance(limit, int)
        ):
            raise ValueError("Invalid MCP search arguments")
        self._service.validate_search(query, offset=offset, limit=limit)
        await self._service.capabilities(self._session_id, server_id=server)
        found = self._service.search(
            self._session_id, query, server_id=server, offset=offset, limit=limit
        )
        matches: list[JsonObject] = []
        for hit in found.hits:
            entry = hit.capability
            matches.append(
                {
                    "name": model_tool_name(entry.server_id, entry.name),
                    "server_id": entry.server_id,
                    "raw_name": entry.name,
                    "description": entry.description,
                    "selection": self._service.select_tool(self._session_id, entry),
                }
            )
        return await self._execution.publish_discovery(
            {
                "matches": normalize_json(matches),
                "complete": found.complete,
                "coverage": normalize_json(
                    json.loads(json.dumps([asdict(item) for item in found.servers]))
                ),
                "next_offset": found.next_offset,
                "suggestion": found.suggestion,
            },
            arguments,
            context,
        )

    def _definition(self, entry: MCPCapability) -> ToolDefinition:
        """根据已加载条目生成工具定义，把执行目标绑定到宿主已核验的服务与目录版本。"""
        raw = require_object(normalize_json(json.loads(entry.definition_json)), "MCP tool")
        schema = require_object(raw["inputSchema"], "MCP input schema")

        async def invoke(arguments: JsonObject, context: ToolExecutionContext) -> ToolOutcome:
            return await self._execution.invoke(entry, arguments, context)

        return ToolDefinition(
            model_tool_name(entry.server_id, entry.name),
            entry.description,
            schema,
            RiskLevel.READ,
            invoke,
            manages_timeout=True,
            origin=ToolOrigin(
                "mcp",
                entry.server_id,
                entry.name,
                entry.generation,
                definition_digest(entry),
                "tool",
            ),
        )
