"""Durable MCP sends and bounded external-content results, independent of RAG."""

import asyncio
import hashlib
import json
from dataclasses import asdict, dataclass

from jsonschema import Draft202012Validator, ValidationError

from jdagent.domain.events import (
    MCPInvocationFinishedPayload,
    MCPInvocationStartedPayload,
    RuntimeEventType,
)
from jdagent.domain.json import JsonObject, normalize_json, require_object, require_string
from jdagent.domain.mcp import MCPCapability, MCPFailure, MCPOperation
from jdagent.domain.tools import (
    ToolErrorCode,
    ToolExecutionContext,
    ToolOrigin,
    ToolOutcome,
    ToolResult,
    ToolResultStatus,
)
from jdagent.mcp_content import MCPContent, project_content
from jdagent.mcp_definitions import definition_digest, model_tool_name
from jdagent.mcp_service import MCPService
from jdagent.ports import ContextObjects, RuntimeJournal


def _encode(value: object) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")


@dataclass(frozen=True, slots=True)
class MCPResultLimits:
    session_bytes: int = 100 << 20
    reservation_bytes: int = (2 << 20) + 16384
    preview_tokens: int = 12000

    def __post_init__(self) -> None:
        if min(self.session_bytes, self.reservation_bytes, self.preview_tokens) <= 0:
            raise ValueError("MCP result limits must be positive")


class MCPExecution:
    """One Turn's use of the authoritative journal and immutable result objects."""

    def __init__(
        self,
        service: MCPService,
        journal: RuntimeJournal,
        objects: ContextObjects,
        limits: MCPResultLimits,
    ) -> None:
        self._service, self._journal, self._objects, self._limits = (
            service,
            journal,
            objects,
            limits,
        )

    def _has_capacity(self, required: int | None = None) -> bool:
        charges: dict[tuple[str | None, str], int] = {}
        for event in self._journal.events:
            payload = event.payload
            if isinstance(payload, MCPInvocationStartedPayload):
                charges[event.turn_id, payload.call_id] = payload.reserved_bytes
            elif isinstance(payload, MCPInvocationFinishedPayload) and payload.result_confirmed:
                charges[event.turn_id, payload.result.call_id] = payload.original_bytes
        reserve = self._limits.reservation_bytes if required is None else required
        return sum(charges.values()) + reserve <= self._limits.session_bytes

    async def publish_discovery(
        self,
        raw: JsonObject,
        arguments: JsonObject,
        context: ToolExecutionContext,
        *,
        name: str = "search_tools",
    ) -> ToolOutcome:
        """Persist a local directory projection using the same quota and preview contract."""
        size = len(_encode(raw))
        if not self._has_capacity(size):
            return ToolOutcome(
                completed=False,
                error_code=ToolErrorCode.CAPACITY,
                message="MCP result storage quota is full",
            )
        origin = ToolOrigin("mcp_control", raw_name=name)
        await self._journal.record(
            context.turn_id,
            RuntimeEventType.MCP_INVOCATION_STARTED,
            MCPInvocationStartedPayload(
                context.call_id,
                "",
                name,
                "",
                context.request_id,
                hashlib.sha256(_encode(arguments)).hexdigest(),
                size,
                operation="discovery",
            ),
        )
        data, digest, size = await self._save(raw, context, origin)
        outcome = ToolOutcome(data=data)
        await self._finish(context, origin, outcome, confirmed=True, digest=digest, size=size)
        return outcome

    async def read_result(
        self, arguments: JsonObject, context: ToolExecutionContext
    ) -> ToolOutcome:
        """Resolve only originals published in this Session; offsets count Unicode characters."""
        digest = require_string(arguments, "sha256")
        offset, limit = arguments.get("offset", 0), arguments.get("limit", 2048)
        if (
            not isinstance(offset, int)
            or not isinstance(limit, int)
            or offset < 0
            or not 1 <= limit <= 2048
        ):
            raise ValueError("Invalid MCP result page")
        record = next(
            (
                event.payload
                for event in self._journal.events
                if event.session_id == context.session_id
                and isinstance(event.payload, MCPInvocationFinishedPayload)
                and event.payload.original_hash == digest
            ),
            None,
        )
        if record is None:
            return ToolOutcome(
                completed=False,
                error_code=ToolErrorCode.TOOL_UNAVAILABLE,
                message="MCP result reference does not belong to this Session",
            )
        content = (await self._objects.get(digest)).decode("utf-8")
        if offset > len(content):
            return ToolOutcome(
                completed=False,
                error_code=ToolErrorCode.INVALID_ARGUMENTS,
                message="MCP result offset is beyond the original",
            )
        end = min(len(content), offset + limit)
        return ToolOutcome(
            data={
                "source": record.result.metadata.get("tool_origin"),
                "original": {
                    "session_id": context.session_id,
                    "sha256": digest,
                    "bytes": record.original_bytes,
                },
                "text": content[offset:end],
                "offset": offset,
                "next_offset": end if end < len(content) else None,
                "complete": end == len(content),
                "format": "json_text_fragment",
            }
        )

    async def invoke(
        self, entry: MCPCapability, arguments: JsonObject, context: ToolExecutionContext
    ) -> ToolOutcome:
        if not self._service.capability_allowed(context.session_id, entry):
            return ToolOutcome(completed=False, error_code=ToolErrorCode.TOOL_UNAVAILABLE)
        if entry.kind == "resource_template":
            return ToolOutcome(completed=False, error_code=ToolErrorCode.UNSUPPORTED)
        operation: MCPOperation = (
            "resources/read"
            if entry.kind == "resource"
            else "prompts/get"
            if entry.kind == "prompt"
            else "tools/call"
        )
        fingerprint = hashlib.sha256(_encode(arguments)).hexdigest()
        # A second model call_id does not erase an unconfirmed send in this Turn.
        confirmed = {
            event.payload.result.call_id
            for event in self._journal.events
            if event.turn_id == context.turn_id
            and isinstance(event.payload, MCPInvocationFinishedPayload)
            and event.payload.result_confirmed
        }
        if any(
            isinstance(event.payload, MCPInvocationStartedPayload)
            and event.turn_id == context.turn_id
            and event.payload.server_id == entry.server_id
            and event.payload.raw_name == entry.name
            and event.payload.operation == operation
            and event.payload.arguments_sha256 == fingerprint
            and event.payload.call_id not in confirmed
            for event in self._journal.events
        ):
            return ToolOutcome(
                completed=False,
                error_code=ToolErrorCode.UNCERTAIN,
                requires_stop=True,
                message="Earlier MCP result is unconfirmed; automatic replay is forbidden",
            )
        if not self._has_capacity():
            return ToolOutcome(
                completed=False,
                error_code=ToolErrorCode.CAPACITY,
                message="MCP result storage quota cannot reserve another response",
            )
        origin = ToolOrigin(
            "mcp",
            entry.server_id,
            entry.name,
            entry.generation,
            definition_digest(entry),
            entry.kind,
        )
        await self._journal.record(
            context.turn_id,
            RuntimeEventType.MCP_INVOCATION_STARTED,
            MCPInvocationStartedPayload(
                context.call_id,
                entry.server_id,
                entry.name,
                entry.generation,
                context.request_id,
                fingerprint,
                self._limits.reservation_bytes,
                operation=operation,
            ),
        )
        try:
            async with asyncio.timeout(self._service.execution_timeout(context.session_id, entry)):
                raw = await self._service.call(context.session_id, entry, arguments)
        except (MCPFailure, TimeoutError) as error:
            message = (
                "MCP local waiting deadline expired"
                if isinstance(error, TimeoutError)
                else str(error)
            )
            outcome = ToolOutcome(
                completed=False,
                error_code=ToolErrorCode.TIMEOUT
                if isinstance(error, TimeoutError)
                else ToolErrorCode.UNCERTAIN,
                message=message,
                requires_stop=True,
            )
            await self._finish(context, origin, outcome, confirmed=False)
            return outcome
        except asyncio.CancelledError:
            outcome = ToolOutcome(
                completed=False,
                cancelled=True,
                error_code=ToolErrorCode.CANCELLED,
                message="MCP waiting cancelled; remote completion is unconfirmed",
                requires_stop=True,
            )
            await self._finish(context, origin, outcome, confirmed=False)
            return outcome
        if len(_encode(raw)) > self._limits.reservation_bytes:
            raise MCPFailure("response_limit")
        content = project_content(raw, entry.kind)
        definition = require_object(
            normalize_json(json.loads(entry.definition_json)), "MCP definition"
        )
        schema = definition.get("outputSchema")
        failed = raw.get("isError") is True
        validate_output = schema is not None and not failed
        invalid = validate_output and "structuredContent" not in raw
        if validate_output:
            if not isinstance(schema, (dict, bool)):
                raise MCPFailure("protocol")
            try:
                Draft202012Validator(schema).validate(raw.get("structuredContent"))  # pyright: ignore[reportUnknownMemberType]
            except ValidationError:
                invalid = True
        unsupported_only = bool(content.unsupported) and not content.usable
        data, digest, size = await self._save(
            raw,
            context,
            origin,
            content=content,
            schema_valid=(not invalid if validate_output else None),
        )
        outcome = ToolOutcome(
            data=data,
            observed_failure=failed or invalid,
            error_code=ToolErrorCode.EXECUTION_FAILED
            if failed or invalid
            else ToolErrorCode.UNSUPPORTED
            if unsupported_only
            else None,
            message="MCP result failed output Schema validation"
            if invalid
            else "MCP tool reported a business failure"
            if failed
            else "MCP result contains only unsupported content"
            if unsupported_only
            else "",
        )
        await self._finish(context, origin, outcome, confirmed=True, digest=digest, size=size)
        return outcome

    async def _save(
        self,
        raw: JsonObject,
        context: ToolExecutionContext,
        origin: ToolOrigin,
        *,
        content: MCPContent | None = None,
        schema_valid: bool | None = None,
    ) -> tuple[JsonObject, str, int]:
        encoded = _encode(raw)
        digest = await self._objects.put(encoded)
        data: JsonObject = {
            "source": normalize_json(asdict(origin)),
            "original": {"session_id": context.session_id, "sha256": digest, "bytes": len(encoded)},
            "preview": raw if content is None else content.preview,
            "preview_complete": True,
            "received_complete": True,
            "processing_complete": content is None or not content.unsupported,
            "unsupported_content": [] if content is None else list(content.unsupported),
            "schema_valid": schema_valid,
        }
        # Use the estimator's uncalibrated UTF-8 byte/token proxy. The request
        # projector can narrow this further to fit the actual remaining window.
        if len(_encode(data)) + 256 > self._limits.preview_tokens:
            projected = _encode(data.pop("preview"))
            data["preview_complete"] = False
            data["preview_format"] = "json_text_fragment"
            room = max(0, self._limits.preview_tokens - len(_encode(data)) - 512)
            fragment = projected[:room].decode("utf-8", errors="ignore")
            data["preview_text"] = fragment
            while len(_encode(data)) + 256 > self._limits.preview_tokens and fragment:
                fragment = fragment[: len(fragment) // 2]
                data["preview_text"] = fragment
        return data, digest, len(encoded)

    async def _finish(
        self,
        context: ToolExecutionContext,
        origin: ToolOrigin,
        outcome: ToolOutcome,
        *,
        confirmed: bool,
        digest: str | None = None,
        size: int = 0,
    ) -> None:
        result = ToolResult(
            context.call_id,
            context.tool_name
            or (
                origin.raw_name or "search_tools"
                if origin.kind == "mcp_control"
                else model_tool_name(origin.server_id or "", origin.raw_name or "")
            ),
            ToolResultStatus.ERROR if outcome.error_code else ToolResultStatus.SUCCESS,
            error_code=outcome.error_code,
            error_message=outcome.message or None,
            metadata={"tool_origin": normalize_json(asdict(origin))},
            outcome=outcome,
        )
        await self._journal.record(
            context.turn_id,
            RuntimeEventType.MCP_INVOCATION_FINISHED,
            MCPInvocationFinishedPayload(result, confirmed, digest, size),
        )
