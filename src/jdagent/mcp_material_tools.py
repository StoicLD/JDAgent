"""Model-selected external resource and Prompt material, without user-role promotion."""

import json
from collections.abc import Callable
from dataclasses import asdict, replace

from jdagent.domain.events import MCPPromptPreparedPayload, RuntimeEventType
from jdagent.domain.json import (
    JsonObject,
    normalize_json,
    require_array,
    require_object,
    require_string,
)
from jdagent.domain.mcp import MCPFailure
from jdagent.domain.tools import (
    RiskLevel,
    ToolDefinition,
    ToolErrorCode,
    ToolExecutionContext,
    ToolOrigin,
    ToolOutcome,
)
from jdagent.mcp_execution import MCPExecution, MCPResultLimits
from jdagent.mcp_service import MCPService
from jdagent.ports import ContextObjects, RuntimeJournal


class MCPMaterialTools:
    """Use the Session catalog for discovery and the shared execution path for content."""

    def __init__(self, service: MCPService, execution: MCPExecution, session_id: str) -> None:
        self._service, self._execution, self._session_id = service, execution, session_id

    def definitions(self) -> tuple[ToolDefinition, ...]:
        definitions: list[ToolDefinition] = []
        page: JsonObject = {
            "server_id": {"type": "string"},
            "offset": {"type": "integer", "minimum": 0},
            "limit": {"type": "integer", "minimum": 1, "maximum": 20},
        }
        if self._service.material_enabled(self._session_id, "resource"):
            definitions.extend(
                (
                    ToolDefinition(
                        "list_mcp_resources",
                        "List external resources or URI templates. "
                        "Listing does not read their contents.",
                        {
                            "type": "object",
                            "properties": {
                                **page,
                                "kind": {
                                    "type": "string",
                                    "enum": ["resource", "resource_template"],
                                },
                            },
                            "additionalProperties": False,
                        },
                        RiskLevel.READ,
                        self._list_resources,
                        manages_timeout=True,
                        origin=ToolOrigin("mcp_control"),
                    ),
                    ToolDefinition(
                        "read_mcp_resource",
                        "Read a concrete URI from an enabled MCP Server. "
                        "Even file: URIs belong to that Server, not the host filesystem.",
                        {
                            "type": "object",
                            "properties": {
                                "server_id": {"type": "string"},
                                "uri": {"type": "string", "minLength": 1},
                            },
                            "required": ["server_id", "uri"],
                            "additionalProperties": False,
                        },
                        RiskLevel.READ,
                        self._read_resource,
                        manages_timeout=True,
                        origin=ToolOrigin("mcp_control"),
                    ),
                )
            )
        if self._service.material_enabled(self._session_id, "prompt"):
            definitions.extend(
                (
                    ToolDefinition(
                        "search_mcp_prompts",
                        "Find external Prompt templates by keywords. A match is only a candidate; "
                        "choose get_mcp_prompt separately when useful.",
                        {
                            "type": "object",
                            "properties": {
                                **page,
                                "query": {"type": "string", "minLength": 1, "maxLength": 512},
                            },
                            "required": ["query"],
                            "additionalProperties": False,
                        },
                        RiskLevel.READ,
                        self._search_prompts,
                        manages_timeout=True,
                        origin=ToolOrigin("mcp_control"),
                    ),
                    ToolDefinition(
                        "get_mcp_prompt",
                        "Fetch a named Prompt with string arguments as external task material. "
                        "Ask the user through normal conversation "
                        "if required arguments are missing.",
                        {
                            "type": "object",
                            "properties": {
                                "server_id": {"type": "string"},
                                "name": {"type": "string", "minLength": 1},
                                "arguments": {
                                    "type": "object",
                                    "additionalProperties": {"type": "string"},
                                },
                            },
                            "required": ["server_id", "name"],
                            "additionalProperties": False,
                        },
                        RiskLevel.READ,
                        self.get_prompt,
                        manages_timeout=True,
                        origin=ToolOrigin("mcp_control"),
                    ),
                )
            )
        return tuple(definitions)

    @staticmethod
    def _page(arguments: JsonObject) -> tuple[str | None, int, int]:
        server = arguments.get("server_id")
        offset, limit = arguments.get("offset", 0), arguments.get("limit", 10)
        if (
            (server is not None and not isinstance(server, str))
            or not isinstance(offset, int)
            or not isinstance(limit, int)
            or offset < 0
            or not 1 <= limit <= 20
        ):
            raise ValueError("Invalid MCP material page")
        return server, offset, limit

    async def _list_resources(
        self, arguments: JsonObject, context: ToolExecutionContext
    ) -> ToolOutcome:
        server, offset, limit = self._page(arguments)
        kind = arguments.get("kind", "resource")
        directory = await self._service.capabilities(self._session_id, server_id=server)
        entries = sorted(
            (entry for entry in directory.entries if entry.kind == kind),
            key=lambda item: (item.server_id, item.name),
        )
        end = offset + limit
        return await self._execution.publish_discovery(
            {
                "entries": normalize_json(
                    [
                        {
                            "server_id": entry.server_id,
                            "kind": entry.kind,
                            "definition": json.loads(entry.definition_json),
                        }
                        for entry in entries[offset:end]
                    ]
                ),
                "complete": directory.complete,
                "coverage": normalize_json(
                    json.loads(json.dumps([asdict(item) for item in directory.servers]))
                ),
                "next_offset": end if end < len(entries) else None,
            },
            arguments,
            context,
            name="list_mcp_resources",
        )

    async def _search_prompts(
        self, arguments: JsonObject, context: ToolExecutionContext
    ) -> ToolOutcome:
        server, offset, limit = self._page(arguments)
        query = require_string(arguments, "query")
        self._service.validate_search(query, offset=offset, limit=limit)
        await self._service.capabilities(self._session_id, server_id=server)
        found = self._service.search(
            self._session_id, query, kind="prompt", server_id=server, offset=offset, limit=limit
        )
        return await self._execution.publish_discovery(
            {
                "matches": normalize_json(
                    [
                        {
                            "server_id": hit.capability.server_id,
                            "definition": json.loads(hit.capability.definition_json),
                        }
                        for hit in found.hits
                    ]
                ),
                "complete": found.complete,
                "coverage": normalize_json(
                    json.loads(json.dumps([asdict(item) for item in found.servers]))
                ),
                "next_offset": found.next_offset,
                "suggestion": found.suggestion,
            },
            arguments,
            context,
            name="search_mcp_prompts",
        )

    async def _read_resource(
        self, arguments: JsonObject, context: ToolExecutionContext
    ) -> ToolOutcome:
        try:
            entry = await self._service.resolve_material(
                self._session_id,
                require_string(arguments, "server_id"),
                "resource",
                require_string(arguments, "uri"),
            )
        except MCPFailure as error:
            return ToolOutcome(
                completed=False, error_code=ToolErrorCode.TOOL_UNAVAILABLE, message=str(error)
            )
        return await self._execution.invoke(entry, {}, context)

    async def get_prompt(self, arguments: JsonObject, context: ToolExecutionContext) -> ToolOutcome:
        """Fetch explicit material with the same required-argument and source contract."""
        try:
            entry = await self._service.resolve_material(
                self._session_id,
                require_string(arguments, "server_id"),
                "prompt",
                require_string(arguments, "name"),
            )
        except MCPFailure as error:
            return ToolOutcome(
                completed=False, error_code=ToolErrorCode.TOOL_UNAVAILABLE, message=str(error)
            )
        supplied = require_object(arguments.get("arguments", {}), "Prompt arguments")
        definition = require_object(
            normalize_json(json.loads(entry.definition_json)), "Prompt definition"
        )
        missing: list[str] = []
        for item in require_array(definition.get("arguments", []), "Prompt arguments"):
            parameter = require_object(item, "Prompt parameter")
            name = require_string(parameter, "name")
            if parameter.get("required") is True and name not in supplied:
                missing.append(name)
        if missing:
            return ToolOutcome(
                completed=False,
                error_code=ToolErrorCode.INVALID_ARGUMENTS,
                message=(
                    "Required Prompt arguments are missing; clarify through normal conversation: "
                    + json.dumps([name[:128] for name in missing[:20]], ensure_ascii=False)
                    + (
                        " (names truncated)"
                        if len(missing) > 20 or any(len(name) > 128 for name in missing)
                        else ""
                    )
                ),
                data={"missing_arguments": normalize_json(missing)},
            )
        return await self._execution.invoke(entry, supplied, context)


class MCPDirectPrompts:
    """Prepare material for a user command, without fabricating a model call pair."""

    def __init__(
        self, service: MCPService, objects: ContextObjects, limits: Callable[[], MCPResultLimits]
    ) -> None:
        self._service, self._objects, self._limits = service, objects, limits

    async def prepare(
        self, journal: RuntimeJournal, arguments: JsonObject, context: ToolExecutionContext
    ) -> ToolOutcome:
        execution = MCPExecution(self._service, journal, self._objects, self._limits())
        outcome = await MCPMaterialTools(self._service, execution, context.session_id).get_prompt(
            arguments, context
        )
        source = outcome.data.get("source") or {
            "kind": "mcp",
            "server_id": require_string(arguments, "server_id"),
            "raw_name": require_string(arguments, "name"),
            "capability_kind": "prompt",
        }
        outcome = replace(outcome, data={**outcome.data, "source": source})
        await journal.record(
            context.turn_id,
            RuntimeEventType.MCP_PROMPT_PREPARED,
            MCPPromptPreparedPayload(context.call_id, outcome),
        )
        return outcome
