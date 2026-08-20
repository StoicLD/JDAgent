"""Readable, append-only JSONL session adapter."""

import asyncio
import json
import os
import re
import threading
from collections.abc import AsyncIterator
from datetime import datetime
from pathlib import Path
from typing import cast

from jdagent.domain.errors import SessionError, SessionErrorCode, StopReason
from jdagent.domain.events import (
    AssistantMessageCompletedPayload,
    ModelUsageRecordedPayload,
    PermissionRequestedPayload,
    PermissionResolvedPayload,
    PermissionRuleGrantedPayload,
    PermissionRuleRevokedPayload,
    RecoverySnapshotPayload,
    RuntimeEvent,
    RuntimeEventType,
    RuntimePayload,
    SessionRenamedPayload,
    SessionStartedPayload,
    ToolCallRequestedPayload,
    ToolExecutionCompletedPayload,
    ToolExecutionStartedPayload,
    TurnCompletedPayload,
    TurnFailedPayload,
    UserMessagePayload,
)
from jdagent.domain.json import (
    JsonObject,
    normalize_json,
    optional_string,
    require_integer,
    require_object,
    require_string,
)
from jdagent.domain.model import MessageRole, ModelMessage, Usage
from jdagent.domain.tools import (
    ApprovalDecision,
    ApprovalRequest,
    PermissionDecision,
    PermissionTargetKind,
    RiskLevel,
    SessionPermissionRule,
    ToolCall,
    ToolErrorCode,
    ToolResult,
    ToolResultStatus,
)

_SESSION_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
_LOCKS_GUARD = threading.Lock()
_PATH_LOCKS: dict[Path, threading.Lock] = {}


def session_file_lock(path: Path) -> threading.Lock:
    """Return the process-local lock shared by session storage adapters."""

    normalized = path.resolve(strict=False)
    with _LOCKS_GUARD:
        return _PATH_LOCKS.setdefault(normalized, threading.Lock())


def _tool_call_to_data(call: ToolCall) -> JsonObject:
    return {"call_id": call.call_id, "name": call.name, "arguments": call.arguments}


def _tool_result_to_data(result: ToolResult) -> JsonObject:
    return {
        "call_id": result.call_id,
        "tool_name": result.tool_name,
        "status": result.status.value,
        "output": result.output,
        "error_code": result.error_code.value if result.error_code else None,
        "error_message": result.error_message,
        "duration_ms": result.duration_ms,
        "metadata": result.metadata,
    }


def _model_message_to_data(message: ModelMessage) -> JsonObject:
    return {
        "role": message.role.value,
        "content": message.content,
        "tool_calls": [_tool_call_to_data(call) for call in message.tool_calls],
        "tool_call_id": message.tool_call_id,
        "name": message.name,
    }


def _payload_to_data(payload: RuntimePayload) -> JsonObject:
    if isinstance(payload, SessionStartedPayload):
        return {
            "name": payload.name,
            "workspace_identity": payload.workspace_identity,
        }
    if isinstance(payload, SessionRenamedPayload):
        return {"name": payload.name}
    if isinstance(payload, RecoverySnapshotPayload):
        return {
            "parent_session_id": payload.parent_session_id,
            "through_sequence": payload.through_sequence,
            "messages": [_model_message_to_data(message) for message in payload.messages],
        }
    if isinstance(payload, UserMessagePayload):
        return {"content": payload.content}
    if isinstance(payload, AssistantMessageCompletedPayload):
        return {
            "content": payload.content,
            "tool_calls": [_tool_call_to_data(call) for call in payload.tool_calls],
        }
    if isinstance(payload, ToolCallRequestedPayload):
        return {"call": _tool_call_to_data(payload.call)}
    if isinstance(payload, PermissionRequestedPayload):
        request = payload.request
        return {
            "request": {
                "tool_name": request.tool_name,
                "arguments": request.arguments,
                "risk": request.risk.value,
                "call_id": request.call_id,
                "session_id": request.session_id,
                "target": request.target,
            }
        }
    if isinstance(payload, PermissionResolvedPayload):
        return {
            "call_id": payload.call_id,
            "policy": payload.policy.value,
            "approval": payload.approval.value if payload.approval else None,
        }
    if isinstance(payload, PermissionRuleGrantedPayload):
        rule = payload.rule
        return {
            "rule": {
                "rule_id": rule.rule_id,
                "session_id": rule.session_id,
                "tool_name": rule.tool_name,
                "target_kind": rule.target_kind.value,
                "target": rule.target,
            }
        }
    if isinstance(payload, PermissionRuleRevokedPayload):
        return {"rule_id": payload.rule_id}
    if isinstance(payload, ToolExecutionStartedPayload):
        return {
            "call_id": payload.call_id,
            "tool_name": payload.tool_name,
            "risk": payload.risk.value if payload.risk is not None else None,
        }
    if isinstance(payload, ToolExecutionCompletedPayload):
        return {"result": _tool_result_to_data(payload.result)}
    if isinstance(payload, ModelUsageRecordedPayload):
        return {
            "provider": payload.provider,
            "model": payload.model,
            "usage": {
                "input_tokens": payload.usage.input_tokens,
                "output_tokens": payload.usage.output_tokens,
                "cached_tokens": payload.usage.cached_tokens,
                "reasoning_tokens": payload.usage.reasoning_tokens,
            },
        }
    if isinstance(payload, TurnCompletedPayload):
        return {
            "stop_reason": payload.stop_reason.value,
            "model_calls": payload.model_calls,
            "tool_calls": payload.tool_calls,
            "provider": payload.provider,
            "model": payload.model,
        }
    return {
        "stop_reason": payload.stop_reason.value,
        "error_category": payload.error_category,
        "message": payload.message,
        "model_calls": payload.model_calls,
        "tool_calls": payload.tool_calls,
        "provider": payload.provider,
        "model": payload.model,
    }


def event_to_data(event: RuntimeEvent) -> JsonObject:
    """Convert one canonical event to its JSON-compatible representation."""

    return {
        "schema_version": event.schema_version,
        "event_id": event.event_id,
        "session_id": event.session_id,
        "turn_id": event.turn_id,
        "sequence": event.sequence,
        "event_type": event.event_type.value,
        "timestamp": event.timestamp.isoformat(),
        "payload": _payload_to_data(event.payload),
    }


def _tool_call(data: JsonObject) -> ToolCall:
    return ToolCall(
        require_string(data, "call_id"),
        require_string(data, "name"),
        require_object(data.get("arguments"), "arguments"),
    )


def _tool_result(data: JsonObject) -> ToolResult:
    error_code = optional_string(data, "error_code")
    return ToolResult(
        require_string(data, "call_id"),
        require_string(data, "tool_name"),
        ToolResultStatus(require_string(data, "status")),
        output=require_string(data, "output"),
        error_code=ToolErrorCode(error_code) if error_code else None,
        error_message=optional_string(data, "error_message"),
        duration_ms=require_integer(data, "duration_ms"),
        metadata=require_object(data.get("metadata"), "metadata"),
    )


def _model_message(data: JsonObject) -> ModelMessage:
    raw_calls = data.get("tool_calls")
    if not isinstance(raw_calls, list):
        raise ValueError("tool_calls must be an array")
    return ModelMessage(
        role=MessageRole(require_string(data, "role")),
        content=require_string(data, "content"),
        tool_calls=tuple(_tool_call(require_object(item, "tool_call")) for item in raw_calls),
        tool_call_id=optional_string(data, "tool_call_id"),
        name=optional_string(data, "name"),
    )


def _payload_from_data(event_type: RuntimeEventType, data: JsonObject) -> RuntimePayload:
    if event_type is RuntimeEventType.SESSION_STARTED:
        return SessionStartedPayload(
            optional_string(data, "name"),
            optional_string(data, "workspace_identity"),
        )
    if event_type is RuntimeEventType.SESSION_RENAMED:
        return SessionRenamedPayload(require_string(data, "name"))
    if event_type is RuntimeEventType.RECOVERY_SNAPSHOT:
        raw_messages = data.get("messages")
        if not isinstance(raw_messages, list):
            raise ValueError("messages must be an array")
        return RecoverySnapshotPayload(
            parent_session_id=require_string(data, "parent_session_id"),
            through_sequence=require_integer(data, "through_sequence"),
            messages=tuple(
                _model_message(require_object(item, "message")) for item in raw_messages
            ),
        )
    if event_type is RuntimeEventType.USER_MESSAGE:
        return UserMessagePayload(require_string(data, "content"))
    if event_type is RuntimeEventType.ASSISTANT_MESSAGE_COMPLETED:
        raw_calls = data.get("tool_calls")
        if not isinstance(raw_calls, list):
            raise ValueError("tool_calls must be an array")
        calls = tuple(_tool_call(require_object(item, "tool_call")) for item in raw_calls)
        return AssistantMessageCompletedPayload(require_string(data, "content"), calls)
    if event_type is RuntimeEventType.TOOL_CALL_REQUESTED:
        return ToolCallRequestedPayload(_tool_call(require_object(data.get("call"), "call")))
    if event_type is RuntimeEventType.PERMISSION_REQUESTED:
        request = require_object(data.get("request"), "request")
        return PermissionRequestedPayload(
            ApprovalRequest(
                require_string(request, "tool_name"),
                require_object(request.get("arguments"), "arguments"),
                RiskLevel(require_string(request, "risk")),
                require_string(request, "call_id"),
                optional_string(request, "session_id") or "",
                optional_string(request, "target"),
            )
        )
    if event_type is RuntimeEventType.PERMISSION_RESOLVED:
        approval = optional_string(data, "approval")
        return PermissionResolvedPayload(
            require_string(data, "call_id"),
            PermissionDecision(require_string(data, "policy")),
            ApprovalDecision(approval) if approval else None,
        )
    if event_type is RuntimeEventType.PERMISSION_RULE_GRANTED:
        rule = require_object(data.get("rule"), "rule")
        return PermissionRuleGrantedPayload(
            SessionPermissionRule(
                rule_id=require_string(rule, "rule_id"),
                session_id=require_string(rule, "session_id"),
                tool_name=require_string(rule, "tool_name"),
                target_kind=PermissionTargetKind(require_string(rule, "target_kind")),
                target=require_string(rule, "target"),
            )
        )
    if event_type is RuntimeEventType.PERMISSION_RULE_REVOKED:
        return PermissionRuleRevokedPayload(require_string(data, "rule_id"))
    if event_type is RuntimeEventType.TOOL_EXECUTION_STARTED:
        risk = optional_string(data, "risk")
        return ToolExecutionStartedPayload(
            require_string(data, "call_id"),
            require_string(data, "tool_name"),
            RiskLevel(risk) if risk is not None else None,
        )
    if event_type is RuntimeEventType.TOOL_EXECUTION_COMPLETED:
        return ToolExecutionCompletedPayload(
            _tool_result(require_object(data.get("result"), "result"))
        )
    if event_type is RuntimeEventType.MODEL_USAGE_RECORDED:
        usage = require_object(data.get("usage"), "usage")
        return ModelUsageRecordedPayload(
            require_string(data, "provider"),
            require_string(data, "model"),
            Usage(
                require_integer(usage, "input_tokens"),
                require_integer(usage, "output_tokens"),
                require_integer(usage, "cached_tokens"),
                require_integer(usage, "reasoning_tokens"),
            ),
        )
    if event_type is RuntimeEventType.TURN_COMPLETED:
        return TurnCompletedPayload(
            StopReason(require_string(data, "stop_reason")),
            require_integer(data, "model_calls"),
            require_integer(data, "tool_calls"),
            optional_string(data, "provider") or "unknown",
            optional_string(data, "model") or "unknown",
        )
    if event_type is RuntimeEventType.TURN_FAILED:
        return TurnFailedPayload(
            StopReason(require_string(data, "stop_reason")),
            require_string(data, "error_category"),
            require_string(data, "message"),
            require_integer(data, "model_calls"),
            require_integer(data, "tool_calls"),
            optional_string(data, "provider") or "unknown",
            optional_string(data, "model") or "unknown",
        )
    raise ValueError(f"Unsupported event type: {event_type.value}")


def event_from_data(data: JsonObject) -> RuntimeEvent:
    """Validate and reconstruct one schema-v1 canonical event."""

    schema_version = require_integer(data, "schema_version")
    if schema_version != 1:
        raise SessionError(
            SessionErrorCode.UNSUPPORTED_SCHEMA,
            f"Unsupported schema version: {schema_version}",
        )
    event_type = RuntimeEventType(require_string(data, "event_type"))
    timestamp = datetime.fromisoformat(require_string(data, "timestamp"))
    if timestamp.tzinfo is None:
        raise ValueError("timestamp must include a timezone")
    return RuntimeEvent(
        schema_version,
        require_string(data, "event_id"),
        require_string(data, "session_id"),
        optional_string(data, "turn_id"),
        require_integer(data, "sequence"),
        event_type,
        timestamp,
        _payload_from_data(event_type, require_object(data.get("payload"), "payload")),
    )


def _decode_json_object(raw: bytes, offset: int) -> JsonObject:
    try:
        loaded = cast(object, json.loads(raw.decode("utf-8")))
        converted = normalize_json(loaded)
        if not isinstance(converted, dict):
            raise ValueError("JSON line must contain an object")
        return converted
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError) as error:
        raise SessionError(
            SessionErrorCode.CORRUPT_EVENT,
            f"Invalid JSONL event at byte offset {offset}",
        ) from error


class JsonlSession:
    """Persist schema-v1 events as one fsynced UTF-8 JSON object per line."""

    def __init__(self, directory: Path) -> None:
        self._directory = directory.resolve(strict=False)
        self._directory.mkdir(parents=True, exist_ok=True)

    async def append(self, event: RuntimeEvent) -> None:
        path = self.path_for(event.session_id)
        try:
            await asyncio.to_thread(self._append_sync, path, event)
        except SessionError:
            raise
        except OSError as error:
            raise SessionError(
                SessionErrorCode.APPEND_FAILED, "Could not append session event"
            ) from error

    async def read(self, session_id: str) -> AsyncIterator[RuntimeEvent]:
        path = self.path_for(session_id)
        events = await asyncio.to_thread(self._read_sync, path, session_id)
        for event in events:
            yield event

    async def list_session_ids(self) -> tuple[str, ...]:
        """List candidate session identifiers without parsing their events."""

        try:
            paths = await asyncio.to_thread(lambda: tuple(self._directory.glob("*.jsonl")))
        except OSError as error:
            raise SessionError(SessionErrorCode.READ_FAILED, "Could not list sessions") from error
        identifiers = (path.stem for path in paths if _SESSION_ID.fullmatch(path.stem) is not None)
        return tuple(sorted(identifiers))

    def path_for(self, session_id: str) -> Path:
        """Resolve a validated session identifier to its storage path."""

        if _SESSION_ID.fullmatch(session_id) is None:
            raise SessionError(SessionErrorCode.NOT_FOUND, "Invalid session ID")
        return self._directory / f"{session_id}.jsonl"

    def _append_sync(self, path: Path, event: RuntimeEvent) -> None:
        line = (
            json.dumps(
                event_to_data(event),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
            + b"\n"
        )
        with session_file_lock(path):
            if path.exists():
                existing = self._read_sync(path, event.session_id)
                expected = len(existing) + 1
            else:
                expected = 1
            if event.sequence != expected:
                raise SessionError(
                    SessionErrorCode.APPEND_FAILED,
                    f"Expected sequence {expected}, received {event.sequence}",
                )
            with path.open("ab") as stream:
                written = stream.write(line)
                if written != len(line):
                    raise SessionError(SessionErrorCode.APPEND_FAILED, "Incomplete event append")
                stream.flush()
                os.fsync(stream.fileno())

    def _read_sync(self, path: Path, session_id: str) -> list[RuntimeEvent]:
        if not path.exists():
            raise SessionError(SessionErrorCode.NOT_FOUND, f"Session not found: {session_id}")
        try:
            raw = path.read_bytes()
        except OSError as error:
            raise SessionError(SessionErrorCode.READ_FAILED, "Could not read session") from error
        if not raw:
            raise SessionError(
                SessionErrorCode.CORRUPT_EVENT,
                "Existing session file is empty at byte offset 0",
            )
        if raw and not raw.endswith(b"\n"):
            offset = raw.rfind(b"\n") + 1
            raise SessionError(
                SessionErrorCode.CORRUPT_EVENT,
                f"Incomplete final JSONL event at byte offset {offset}",
            )
        events: list[RuntimeEvent] = []
        offset = 0
        for line in raw.splitlines(keepends=True):
            data = _decode_json_object(line[:-1], offset)
            try:
                event = event_from_data(data)
            except SessionError:
                raise
            except (ValueError, TypeError) as error:
                raise SessionError(
                    SessionErrorCode.CORRUPT_EVENT,
                    f"Invalid event schema at byte offset {offset}",
                ) from error
            expected = len(events) + 1
            if event.session_id != session_id or event.sequence != expected:
                raise SessionError(
                    SessionErrorCode.CORRUPT_EVENT,
                    f"Invalid session or sequence at byte offset {offset}",
                )
            events.append(event)
            offset += len(line)
        return events
