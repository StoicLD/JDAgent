"""Durable MCP sends and bounded external-content results, independent of RAG."""

import asyncio
import hashlib
import json
from dataclasses import asdict, dataclass, replace

from jsonschema import Draft202012Validator, ValidationError

from jdagent.cancellation import complete_before_cancel
from jdagent.domain.events import (
    MCPAttemptFinishedPayload,
    MCPInvocationFinishedPayload,
    MCPInvocationStartedPayload,
    MCPTaskObservedPayload,
    MCPTaskRegisteredPayload,
    MCPTaskResultReservedPayload,
    MCPTaskResultSavedPayload,
    RuntimeEventType,
)
from jdagent.domain.json import JsonObject, normalize_json, require_object, require_string
from jdagent.domain.mcp import MCPCapability, MCPFailure, MCPOperation, MCPTask
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
from jdagent.mcp_task_execution import MCPTaskExecution
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

    def _usage(self) -> tuple[int, dict[tuple[str | None, str], int]]:
        charges: dict[tuple[str | None, str], int] = {}
        objects: dict[str, int] = {}
        tasks: dict[str, tuple[str | None, str]] = {}
        for event in self._journal.events:
            payload = event.payload
            if isinstance(payload, MCPInvocationStartedPayload):
                charges[event.turn_id, payload.call_id] = payload.reserved_bytes
            elif isinstance(payload, MCPTaskRegisteredPayload):
                key = (event.turn_id, payload.call_id)
                charges[key] = max(0, charges.get(key, 0) - payload.handle_bytes)
                objects[payload.handle_hash] = max(
                    objects.get(payload.handle_hash, 0), payload.handle_bytes
                )
                tasks[payload.task_ref] = key
            elif (
                isinstance(payload, MCPTaskObservedPayload)
                and payload.response_confirmed
                and payload.status in {"failed", "cancelled"}
                and payload.task_ref in tasks
            ):
                # These terminal states have no business result to fetch. Keep
                # the control object charged, but release its unused reservation.
                charges[tasks[payload.task_ref]] = 0
            elif isinstance(payload, MCPInvocationFinishedPayload):
                if payload.result_confirmed:
                    charges[event.turn_id, payload.result.call_id] = 0
                if payload.original_hash is not None:
                    objects[payload.original_hash] = max(
                        objects.get(payload.original_hash, 0), payload.original_bytes
                    )
            elif isinstance(payload, MCPTaskResultSavedPayload):
                charges[payload.origin_turn_id, payload.call_id] = 0
                objects[payload.original_hash] = max(
                    objects.get(payload.original_hash, 0), payload.original_bytes
                )
            elif isinstance(payload, MCPTaskResultReservedPayload):
                charges[payload.origin_turn_id, payload.call_id] = payload.reserved_bytes
        return sum(charges.values()) + sum(objects.values()), charges

    def _has_capacity(self, required: int | None = None) -> bool:
        used, _ = self._usage()
        reserve = self._limits.reservation_bytes if required is None else required
        return used + reserve <= self._limits.session_bytes

    async def reserve_task_result(self, task_ref: str, context: ToolExecutionContext) -> bool:
        """Retain existing capacity or commit new capacity before a management RPC."""
        used, reservations = self._usage()
        existing = reservations.get((context.turn_id, context.call_id), 0)
        required = self._limits.reservation_bytes
        if existing >= required:
            return True
        if used + required - existing > self._limits.session_bytes:
            return False
        await self._journal.record(
            None,
            RuntimeEventType.MCP_TASK_RESULT_RESERVED,
            MCPTaskResultReservedPayload(task_ref, context.call_id, context.turn_id, required),
        )
        return True

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
                and isinstance(
                    event.payload, (MCPInvocationFinishedPayload, MCPTaskResultSavedPayload)
                )
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
                "source": record.result.metadata.get("tool_origin")
                if isinstance(record, MCPInvocationFinishedPayload)
                else record.outcome.data.get("source"),
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
        stopped = {
            event.payload.result.call_id
            for event in self._journal.events
            if event.turn_id == context.turn_id
            and isinstance(event.payload, MCPInvocationFinishedPayload)
            and event.payload.result.stop_turn
        }
        prior_records = [
            (event.sequence, event.payload)
            for event in self._journal.events
            if isinstance(event.payload, MCPInvocationStartedPayload)
            and event.turn_id == context.turn_id
            and event.payload.server_id == entry.server_id
            and event.payload.raw_name == entry.name
            and event.payload.generation == entry.generation
            and event.payload.operation == operation
            and event.payload.arguments_sha256 == fingerprint
        ]
        prior = [item for _, item in prior_records]
        if any(item.call_id not in confirmed or item.call_id in stopped for item in prior):
            return ToolOutcome(
                completed=False,
                error_code=ToolErrorCode.UNCERTAIN,
                requires_stop=True,
                message=(
                    "Earlier MCP result is unconfirmed or stopped; automatic replay is forbidden"
                ),
            )
        prior_ids = {item.call_id for item in prior}
        last_success = max(
            (
                event.sequence
                for event in self._journal.events
                if event.turn_id == context.turn_id
                and isinstance(event.payload, MCPInvocationFinishedPayload)
                and event.payload.result.call_id in prior_ids
                and event.payload.result_confirmed
                and event.payload.result.status is ToolResultStatus.SUCCESS
                and (event.payload.result.outcome is None or event.payload.result.outcome.completed)
            ),
            default=0,
        )
        # Success closes a logical operation. Failed attempts cannot be reset by
        # another model call_id, but independent later successful reads remain usable.
        prior = [item for sequence, item in prior_records if sequence > last_success]
        attempted = len(prior) if entry.kind == "tool" else 0
        safe_repeat = self._service.safe_repeat(context.session_id, entry)
        previous_timeout = any(
            isinstance(event.payload, MCPAttemptFinishedPayload)
            and event.turn_id == context.turn_id
            and event.payload.call_id in {item.call_id for item in prior}
            and event.payload.failure_kind == "server_timeout"
            for event in self._journal.events
        )
        if prior and (attempted >= 3 or not previous_timeout or not safe_repeat):
            return ToolOutcome(
                completed=False,
                error_code=ToolErrorCode.NOT_EXECUTED,
                message="MCP automatic replay is forbidden or its attempt allowance is exhausted",
                requires_stop=True,
            )
        reservation = self._limits.reservation_bytes * (
            2 if self._service.supports_tasks(context.session_id, entry) else 1
        )
        if not self._has_capacity(reservation):
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
        waiting_task = False
        raw: JsonObject | MCPTask | None = None
        task_execution = MCPTaskExecution(
            self._service,
            self._journal,
            self._objects,
            max_control_bytes=self._limits.reservation_bytes,
        )
        self._service.begin_activity(context.session_id, entry, operation)
        try:
            async with asyncio.timeout(self._service.execution_timeout(context.session_id, entry)):
                raw, attempt = await self._call_with_retries(
                    entry,
                    arguments,
                    context,
                    operation,
                    fingerprint,
                    attempted,
                    safe_repeat,
                    reservation,
                )
                if isinstance(raw, MCPTask):
                    waiting_task = True
                    await task_execution.register(entry, raw, context)
                await self._journal.record(
                    context.turn_id,
                    RuntimeEventType.MCP_ATTEMPT_FINISHED,
                    MCPAttemptFinishedPayload(context.call_id, attempt, True),
                )
                if isinstance(raw, MCPTask):
                    final = await task_execution.wait(entry, raw, context)
                    if isinstance(final, ToolOutcome):
                        if task_execution.final_result is not None:
                            return await self._complete(
                                entry,
                                task_execution.final_result,
                                context,
                                origin,
                                task_execution.details,
                                interruption=final,
                            )
                        await self._finish(
                            context, origin, final, confirmed=task_execution.result_confirmed
                        )
                        return final
                    raw = final
        except (MCPFailure, TimeoutError) as error:
            if isinstance(error, TimeoutError):
                await task_execution.cancel(entry, context)
            server_timeout = isinstance(error, MCPFailure) and error.kind == "server_timeout"
            confirmed_timeout = (
                server_timeout and not waiting_task
            ) or self._last_attempt_timed_out_remotely(context)
            message = (
                "MCP local waiting deadline expired"
                if isinstance(error, TimeoutError)
                else str(error)
            )
            outcome = ToolOutcome(
                data={
                    **task_execution.details,
                    "timeout_source": "server" if server_timeout else "client",
                }
                if server_timeout or isinstance(error, TimeoutError)
                else task_execution.details,
                completed=False,
                error_code=ToolErrorCode.TIMEOUT
                if server_timeout or isinstance(error, TimeoutError)
                else ToolErrorCode.UNCERTAIN,
                message=message,
                observed_failure=confirmed_timeout,
                requires_stop=not server_timeout or waiting_task,
            )
            received = task_execution.final_result if waiting_task else raw
            if isinstance(received, dict):
                return await self._complete(
                    entry,
                    received,
                    context,
                    origin,
                    task_execution.details,
                    interruption=outcome,
                )
            await self._finish(
                context,
                origin,
                outcome,
                confirmed=confirmed_timeout or task_execution.result_confirmed,
            )
            return outcome
        except asyncio.CancelledError:
            await task_execution.cancel(entry, context)
            confirmed_timeout = self._last_attempt_timed_out_remotely(context)
            outcome = ToolOutcome(
                data=task_execution.details,
                completed=False,
                cancelled=True,
                error_code=ToolErrorCode.CANCELLED,
                message="MCP waiting cancelled; remote result was received"
                if isinstance(raw, dict) and not waiting_task
                else "MCP waiting cancelled; remote Task termination confirmed"
                if task_execution.result_confirmed
                else "MCP waiting cancelled after a confirmed server timeout"
                if confirmed_timeout
                else "MCP waiting cancelled; remote completion is unconfirmed",
                observed_failure=confirmed_timeout,
                requires_stop=True,
            )
            received = task_execution.final_result if waiting_task else raw
            if isinstance(received, dict):
                return await self._complete(
                    entry,
                    received,
                    context,
                    origin,
                    task_execution.details,
                    interruption=outcome,
                )
            await self._finish(
                context,
                origin,
                outcome,
                confirmed=confirmed_timeout or task_execution.result_confirmed,
            )
            return outcome
        else:
            return await self._complete(entry, raw, context, origin, task_execution.details)
        finally:
            self._service.end_activity(context.session_id, entry)

    async def _complete(
        self,
        entry: MCPCapability,
        raw: JsonObject,
        context: ToolExecutionContext,
        origin: ToolOrigin,
        task_details: JsonObject,
        *,
        interruption: ToolOutcome | None = None,
        management_task_ref: str | None = None,
    ) -> ToolOutcome:
        """Retain a received result even when completion races local cancellation."""
        published: ToolOutcome | None = None

        async def publish() -> None:
            nonlocal published
            published = await self._publish(
                entry,
                raw,
                context,
                origin,
                task_details,
                interruption=interruption,
                management_task_ref=management_task_ref,
            )

        # This transaction performs only local object/event writes, never RPCs.
        # Once a response is received, cancelling its disk wait must not orphan
        # an ongoing write or erase the reference to that known result.
        try:
            await complete_before_cancel(publish())
        except asyncio.CancelledError:
            if published is None:
                raise
            return replace(
                published,
                cancelled=True,
                error_code=ToolErrorCode.CANCELLED,
                requires_stop=True,
                message="MCP waiting cancelled; remote result was saved",
            )
        assert published is not None
        return published

    async def _publish(
        self,
        entry: MCPCapability,
        raw: JsonObject,
        context: ToolExecutionContext,
        origin: ToolOrigin,
        task_details: JsonObject,
        *,
        interruption: ToolOutcome | None,
        management_task_ref: str | None,
    ) -> ToolOutcome:
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
        data.update(task_details)
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
        if interruption is not None:
            outcome = replace(
                interruption,
                data={**data, **interruption.data, "result_received_during_cleanup": True},
                observed_failure=interruption.observed_failure or failed or invalid,
            )
        if management_task_ref is not None:
            await self._journal.record(
                None,
                RuntimeEventType.MCP_TASK_RESULT_SAVED,
                MCPTaskResultSavedPayload(
                    management_task_ref, context.call_id, context.turn_id, outcome, digest, size
                ),
            )
        else:
            await self._finish(context, origin, outcome, confirmed=True, digest=digest, size=size)
        return outcome

    async def publish_task_result(
        self,
        entry: MCPCapability,
        raw: JsonObject,
        context: ToolExecutionContext,
        details: JsonObject,
    ) -> ToolOutcome:
        """Save an explicitly retrieved result without repeating the original call's completion."""
        origin = ToolOrigin(
            "mcp", entry.server_id, entry.name, entry.generation, definition_digest(entry), "tool"
        )
        return await self._complete(
            entry,
            raw,
            context,
            origin,
            details,
            management_task_ref=require_string(details, "task_ref"),
        )

    def _last_attempt_timed_out_remotely(self, context: ToolExecutionContext) -> bool:
        """Stopping during backoff must not erase the already-confirmed remote failure."""
        for event in reversed(self._journal.events):
            if event.turn_id != context.turn_id:
                continue
            payload = event.payload
            if isinstance(payload, (MCPAttemptFinishedPayload, MCPInvocationStartedPayload)):
                if payload.call_id == context.call_id:
                    return (
                        isinstance(payload, MCPAttemptFinishedPayload)
                        and payload.response_confirmed
                        and payload.failure_kind == "server_timeout"
                    )
        return False

    async def _call_with_retries(
        self,
        entry: MCPCapability,
        arguments: JsonObject,
        context: ToolExecutionContext,
        operation: MCPOperation,
        fingerprint: str,
        attempted: int,
        safe_repeat: bool,
        reservation: int,
    ) -> tuple[JsonObject | MCPTask, int]:
        for attempt in range(attempted + 1, 4):
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
                    reservation,
                    attempt=attempt,
                    operation=operation,
                ),
            )
            try:
                raw = await self._service.call(context.session_id, entry, arguments)
            except MCPFailure as error:
                await self._journal.record(
                    context.turn_id,
                    RuntimeEventType.MCP_ATTEMPT_FINISHED,
                    MCPAttemptFinishedPayload(
                        context.call_id,
                        attempt,
                        error.kind == "server_timeout",
                        error.kind,
                        error.rpc_code,
                    ),
                )
                if error.kind != "server_timeout" or not safe_repeat or attempt == 3:
                    raise
                await asyncio.sleep(0.5 * 2 ** (attempt - 1))
            except asyncio.CancelledError:
                await self._journal.record(
                    context.turn_id,
                    RuntimeEventType.MCP_ATTEMPT_FINISHED,
                    MCPAttemptFinishedPayload(context.call_id, attempt, False, "interrupted"),
                )
                raise
            else:
                # Return the receipt before another cancellable persistence or
                # observer boundary, so invoke can preserve it during cleanup.
                return raw, attempt
        raise RuntimeError("MCP attempt guard did not reject an exhausted operation")

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
