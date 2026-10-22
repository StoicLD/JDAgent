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

from jdagent.adapters.context_objects import FileContextObjects
from jdagent.adapters.session_lock import SessionLease
from jdagent.domain.errors import SessionError, SessionErrorCode, StopReason
from jdagent.domain.events import (
    AssistantMessageCompletedPayload,
    CitationRecord,
    CompactionCommittedPayload,
    CompactionFailedPayload,
    ConstraintRevokedPayload,
    ContextWindowSelectedPayload,
    ModelRequestPreparedPayload,
    ModelUsageRecordedPayload,
    PermissionRequestedPayload,
    PermissionResolvedPayload,
    PermissionRuleGrantedPayload,
    PermissionRuleRevokedPayload,
    RecoverySnapshotPayload,
    RetrievalBaseRecord,
    RetrievalBudgetRecord,
    RetrievalEvidenceRecord,
    RuntimeEvent,
    RuntimeEventType,
    RuntimePayload,
    SessionRenamedPayload,
    SessionStartedPayload,
    SideEffectResolutionPayload,
    ToolCallRequestedPayload,
    ToolExecutionCompletedPayload,
    ToolExecutionStartedPayload,
    TurnCompletedPayload,
    TurnFailedPayload,
    TurnRetrievalRecordedPayload,
    UserMessagePayload,
)
from jdagent.domain.json import (
    JsonObject,
    normalize_json,
    optional_string,
    require_array,
    require_integer,
    require_object,
    require_string,
)
from jdagent.domain.model import Usage
from jdagent.domain.tools import (
    ApprovalDecision,
    ApprovalRequest,
    PermissionDecision,
    PermissionTargetKind,
    RiskLevel,
    SessionPermissionRule,
    ToolCall,
    ToolErrorCode,
    ToolOutcome,
    ToolResult,
    ToolResultStatus,
)

_SESSION_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
_LOCKS_GUARD = threading.Lock()
_PATH_LOCKS: dict[Path, threading.RLock] = {}


def session_file_lock(path: Path) -> threading.RLock:
    """Return the process-local lock shared by session storage adapters."""

    normalized = path.resolve(strict=False)
    with _LOCKS_GUARD:
        return _PATH_LOCKS.setdefault(normalized, threading.RLock())


def _tool_call_to_data(call: ToolCall) -> JsonObject:
    return {"call_id": call.call_id, "name": call.name, "arguments": call.arguments}


def _citation_to_data(item: CitationRecord) -> JsonObject:
    return {
        "ordinal": item.ordinal,
        "evidence_id": item.evidence_id,
        "knowledge_base_id": item.knowledge_base_id,
        "source_id": item.source_id,
        "source_version_id": item.source_version_id,
        "snapshot_id": item.snapshot_id,
        "locator": item.locator,
        "locator_schema_version": item.locator_schema_version,
        "content_hash": item.content_hash,
    }


def _base_to_data(item: RetrievalBaseRecord) -> JsonObject:
    return {
        "binding_id": item.binding_id,
        "connection_id": item.connection_id,
        "knowledge_base_id": item.knowledge_base_id,
        "kb_name": item.kb_name,
        "status": item.status,
        "failure_reason": item.failure_reason,
        "generation_id": item.generation_id,
        "revision": item.revision,
        "physical_collection": item.physical_collection,
        "embedding_profile_fingerprint": item.embedding_profile_fingerprint,
        "retrieval_profile_fingerprint": item.retrieval_profile_fingerprint,
        "hit_count": item.hit_count,
        "selected_evidence_count": item.selected_evidence_count,
    }


def _evidence_to_data(item: RetrievalEvidenceRecord) -> JsonObject:
    return {
        "evidence_id": item.evidence_id,
        "ordinal": item.ordinal,
        "knowledge_base_id": item.knowledge_base_id,
        "source_id": item.source_id,
        "source_version_id": item.source_version_id,
        "snapshot_id": item.snapshot_id,
        "locator": item.locator,
        "locator_schema_version": item.locator_schema_version,
        "content_hash": item.content_hash,
    }


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
        "outcome": result.outcome.to_data() if result.outcome else None,
    }


def _payload_to_data(payload: RuntimePayload) -> JsonObject:
    if isinstance(payload, SideEffectResolutionPayload):
        return {
            "parent_session_id": payload.parent_session_id,
            "call_id": payload.call_id,
            "user_event_id": payload.user_event_id,
            "conclusion": payload.conclusion,
            "evidence": payload.evidence,
        }
    if isinstance(payload, ConstraintRevokedPayload):
        return {
            "constraint_sequence": payload.constraint_sequence,
            "user_event_id": payload.user_event_id,
            "expected_version": payload.expected_version,
        }
    if isinstance(payload, ModelRequestPreparedPayload):
        return {
            "request_id": payload.request_id,
            "object_hash": payload.object_hash,
            "call_kind": payload.call_kind,
            "input_tokens": payload.input_tokens,
        }
    if isinstance(payload, CompactionCommittedPayload):
        return {
            "through_sequence": payload.through_sequence,
            "prefix_hash": payload.prefix_hash,
            "object_hash": payload.object_hash,
        }
    if isinstance(payload, CompactionFailedPayload):
        return {
            "through_sequence": payload.through_sequence,
            "prefix_hash": payload.prefix_hash,
            "reason": payload.reason,
            "response_hashes": list(payload.response_hashes),
        }
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
            "prefix_hash": payload.prefix_hash,
            "checkpoint_hash": payload.checkpoint_hash,
            "unknown_operations": list(payload.unknown_operations),
            "gap_until": payload.gap_until,
        }
    if isinstance(payload, UserMessagePayload):
        return {"content": payload.content}
    if isinstance(payload, ContextWindowSelectedPayload):
        return {"omitted_turn_ids": list(payload.omitted_turn_ids)}
    if isinstance(payload, TurnRetrievalRecordedPayload):
        return {
            "outcome": payload.outcome,
            "turn_token": payload.turn_token,
            "bases": [_base_to_data(item) for item in payload.bases],
            "evidence": [_evidence_to_data(item) for item in payload.evidence],
            "budget": {
                "dense_top_k": payload.budget.dense_top_k,
                "bm25_top_k": payload.budget.bm25_top_k,
                "fused_child_count": payload.budget.fused_child_count,
                "rerank_pool_size": payload.budget.rerank_pool_size,
                "parent_count": payload.budget.parent_count,
                "evidence_tokens": payload.budget.evidence_tokens,
                "evidence_token_limit": payload.budget.evidence_token_limit,
                "parent_limit": payload.budget.parent_limit,
            },
            "degradations": list(payload.degradations),
        }
    if isinstance(payload, AssistantMessageCompletedPayload):
        return {
            "content": payload.content,
            "tool_calls": [_tool_call_to_data(call) for call in payload.tool_calls],
            "citations": [_citation_to_data(item) for item in payload.citations],
            "model_supplement": payload.model_supplement,
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
                "args_prefix": list(rule.args_prefix),
                "cwd": rule.cwd,
                "environment_id": rule.environment_id,
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
            "request_id": payload.request_id,
            "call_kind": payload.call_kind,
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


def _citation_from_data(data: JsonObject) -> CitationRecord:
    return CitationRecord(
        require_integer(data, "ordinal"),
        require_string(data, "evidence_id"),
        require_string(data, "knowledge_base_id"),
        require_string(data, "source_id"),
        require_string(data, "source_version_id"),
        require_string(data, "snapshot_id"),
        require_string(data, "locator"),
        require_integer(data, "locator_schema_version"),
        require_string(data, "content_hash"),
    )


def _base_from_data(data: JsonObject) -> RetrievalBaseRecord:
    return RetrievalBaseRecord(
        require_string(data, "binding_id"),
        require_string(data, "connection_id"),
        require_string(data, "knowledge_base_id"),
        require_string(data, "kb_name"),
        require_string(data, "status"),
        optional_string(data, "failure_reason"),
        optional_string(data, "generation_id"),
        require_integer(data, "revision"),
        optional_string(data, "physical_collection"),
        require_string(data, "embedding_profile_fingerprint"),
        require_string(data, "retrieval_profile_fingerprint"),
        require_integer(data, "hit_count"),
        require_integer(data, "selected_evidence_count"),
    )


def _evidence_record_from_data(data: JsonObject) -> RetrievalEvidenceRecord:
    return RetrievalEvidenceRecord(
        require_string(data, "evidence_id"),
        require_integer(data, "ordinal"),
        require_string(data, "knowledge_base_id"),
        require_string(data, "source_id"),
        require_string(data, "source_version_id"),
        require_string(data, "snapshot_id"),
        require_string(data, "locator"),
        require_integer(data, "locator_schema_version"),
        require_string(data, "content_hash"),
    )


def _tool_result(data: JsonObject) -> ToolResult:
    error_code = optional_string(data, "error_code")
    raw_outcome = data.get("outcome")
    outcome = None
    if raw_outcome is not None:
        value = require_object(raw_outcome, "outcome")
        for key in ("completed", "observed_failure", "cancelled", "requires_stop"):
            if not isinstance(value.get(key), bool):
                raise ValueError(f"{key} must be boolean")
        code = optional_string(value, "error_code")
        outcome = ToolOutcome(
            data=require_object(value.get("data"), "data"),
            completed=value["completed"] is True,
            effect=require_string(value, "effect"),
            observed_failure=value["observed_failure"] is True,
            error_code=ToolErrorCode(code) if code else None,
            message=require_string(value, "message"),
            cancelled=value["cancelled"] is True,
            requires_stop=value.get("requires_stop") is True,
        )
    return ToolResult(
        require_string(data, "call_id"),
        require_string(data, "tool_name"),
        ToolResultStatus(require_string(data, "status")),
        output=require_string(data, "output"),
        error_code=ToolErrorCode(error_code) if error_code else None,
        error_message=optional_string(data, "error_message"),
        duration_ms=require_integer(data, "duration_ms"),
        metadata=require_object(data.get("metadata"), "metadata"),
        outcome=outcome,
    )


def _payload_from_data(event_type: RuntimeEventType, data: JsonObject) -> RuntimePayload:
    if event_type is RuntimeEventType.SIDE_EFFECT_RESOLVED:
        return SideEffectResolutionPayload(
            require_string(data, "parent_session_id"),
            require_string(data, "call_id"),
            require_string(data, "user_event_id"),
            require_string(data, "conclusion"),
            require_string(data, "evidence"),
        )
    if event_type is RuntimeEventType.CONSTRAINT_REVOKED:
        return ConstraintRevokedPayload(
            require_integer(data, "constraint_sequence"),
            require_string(data, "user_event_id"),
            require_integer(data, "expected_version"),
        )
    if event_type is RuntimeEventType.MODEL_REQUEST_PREPARED:
        return ModelRequestPreparedPayload(
            require_string(data, "request_id"),
            require_string(data, "object_hash"),
            require_string(data, "call_kind"),
            require_integer(data, "input_tokens"),
        )
    if event_type is RuntimeEventType.COMPACTION_COMMITTED:
        return CompactionCommittedPayload(
            require_integer(data, "through_sequence"),
            require_string(data, "prefix_hash"),
            require_string(data, "object_hash"),
        )
    if event_type is RuntimeEventType.COMPACTION_FAILED:
        raw_responses = require_array(data.get("response_hashes", []), "response_hashes")
        if not all(isinstance(item, str) for item in raw_responses):
            raise ValueError("Compaction response hashes must be strings")
        return CompactionFailedPayload(
            require_integer(data, "through_sequence"),
            require_string(data, "prefix_hash"),
            require_string(data, "reason"),
            tuple(str(item) for item in raw_responses),
        )
    if event_type is RuntimeEventType.SESSION_STARTED:
        return SessionStartedPayload(
            optional_string(data, "name"),
            optional_string(data, "workspace_identity"),
        )
    if event_type is RuntimeEventType.SESSION_RENAMED:
        return SessionRenamedPayload(require_string(data, "name"))
    if event_type is RuntimeEventType.RECOVERY_SNAPSHOT:
        raw_operations = require_array(data.get("unknown_operations"), "unknown_operations")
        if not all(isinstance(item, str) for item in raw_operations):
            raise ValueError("Invalid unknown operations")
        return RecoverySnapshotPayload(
            require_string(data, "parent_session_id"),
            require_integer(data, "through_sequence"),
            require_string(data, "prefix_hash"),
            optional_string(data, "checkpoint_hash"),
            tuple(str(item) for item in raw_operations),
            require_integer(data, "gap_until"),
        )
    if event_type is RuntimeEventType.USER_MESSAGE:
        return UserMessagePayload(require_string(data, "content"))
    if event_type is RuntimeEventType.TURN_RETRIEVAL_RECORDED:
        raw_bases = data.get("bases")
        raw_evidence = data.get("evidence")
        raw_degradations = data.get("degradations")
        if not isinstance(raw_bases, list) or not isinstance(raw_evidence, list):
            raise ValueError("turn_retrieval_recorded lists are invalid")
        if not isinstance(raw_degradations, list):
            raise ValueError("degradations must be an array")
        budget = require_object(data.get("budget"), "budget")
        return TurnRetrievalRecordedPayload(
            require_string(data, "outcome"),
            require_string(data, "turn_token"),
            tuple(_base_from_data(require_object(item, "base")) for item in raw_bases),
            tuple(
                _evidence_record_from_data(require_object(item, "evidence"))
                for item in raw_evidence
            ),
            RetrievalBudgetRecord(
                require_integer(budget, "dense_top_k"),
                require_integer(budget, "bm25_top_k"),
                require_integer(budget, "fused_child_count"),
                require_integer(budget, "rerank_pool_size"),
                require_integer(budget, "parent_count"),
                require_integer(budget, "evidence_tokens"),
                require_integer(budget, "evidence_token_limit"),
                require_integer(budget, "parent_limit"),
            ),
            tuple(str(item) for item in raw_degradations),
        )
    if event_type is RuntimeEventType.ASSISTANT_MESSAGE_COMPLETED:
        raw_calls = data.get("tool_calls")
        if not isinstance(raw_calls, list):
            raise ValueError("tool_calls must be an array")
        calls = tuple(_tool_call(require_object(item, "tool_call")) for item in raw_calls)
        raw_citations = data.get("citations")
        if raw_citations is None:
            citations: tuple[CitationRecord, ...] = ()
        elif not isinstance(raw_citations, list):
            raise ValueError("citations must be an array")
        else:
            citations = tuple(
                _citation_from_data(require_object(item, "citation")) for item in raw_citations
            )
        return AssistantMessageCompletedPayload(
            require_string(data, "content"),
            calls,
            citations,
            optional_string(data, "model_supplement") or "",
        )
    if event_type is RuntimeEventType.TOOL_CALL_REQUESTED:
        return ToolCallRequestedPayload(_tool_call(require_object(data.get("call"), "call")))
    if event_type is RuntimeEventType.CONTEXT_WINDOW_SELECTED:
        return ContextWindowSelectedPayload(
            tuple(
                require_string({"id": value}, "id")
                for value in require_array(data.get("omitted_turn_ids"), "omitted_turn_ids")
            )
        )
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
                args_prefix=tuple(
                    require_string({"arg": value}, "arg")
                    for value in require_array(rule.get("args_prefix", []), "args_prefix")
                ),
                cwd=optional_string(rule, "cwd") or "",
                environment_id=optional_string(rule, "environment_id") or "",
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
            optional_string(data, "request_id"),
            require_string(data, "call_kind"),
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
    """Validate and reconstruct one schema-v2 canonical event."""

    schema_version = require_integer(data, "schema_version")
    if schema_version != 4:
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
    """Persist schema-v2 events as one fsynced UTF-8 JSON object per line."""

    def __init__(self, directory: Path) -> None:
        self._directory = directory.resolve(strict=False)
        self._directory.mkdir(parents=True, exist_ok=True)
        self._leases: dict[str, SessionLease] = {}
        self._lease_guard = threading.RLock()
        self._tails: dict[str, tuple[int, int, int, int]] = {}
        self._objects = FileContextObjects(self._directory / "objects")

    def decode_record(self, data: JsonObject) -> RuntimeEvent:
        """Hydrate only the envelope's owned payload, never user-controlled nested keys."""
        if "owned_payload_hash" in data:
            raw = self._objects.get_sync(require_string(data, "owned_payload_hash"))
            if len(raw) != require_integer(data, "payload_bytes"):
                raise ValueError("Session payload size mismatch")
            data = {**data, "payload": _decode_json_object(raw, 0)}
        return event_from_data(data)

    async def acquire(self, session_id: str) -> None:
        await asyncio.to_thread(self._acquire_sync, session_id)

    def owns(self, session_id: str) -> bool:
        with self._lease_guard:
            return session_id in self._leases

    def _acquire_sync(self, session_id: str) -> None:
        with self._lease_guard:
            path = self.path_for(session_id)
            if session_id not in self._leases:
                self._leases[session_id] = SessionLease(path.with_suffix(".lock"))
                self._tails.pop(session_id, None)

    async def release(self, session_id: str) -> None:
        await asyncio.to_thread(self._release_sync, session_id)

    def _release_sync(self, session_id: str) -> None:
        with self._lease_guard:
            lease = self._leases.pop(session_id, None)
            self._tails.pop(session_id, None)
            if lease is not None:
                lease.close()

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
        with session_file_lock(path), self._lease_guard:
            temporary = event.session_id not in self._leases
            self._acquire_sync(event.session_id)
            try:
                self._append_owned(path, event)
            finally:
                if temporary:
                    self._release_sync(event.session_id)

    def _append_owned(self, path: Path, event: RuntimeEvent) -> None:
        data = event_to_data(event)
        payload = json.dumps(
            data["payload"], ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        if len(payload) > 16384:
            data.pop("payload")
            data["owned_payload_hash"] = self._objects.put_sync(payload)
            data["payload_bytes"] = len(payload)
        line = (
            json.dumps(
                data,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
            + b"\n"
        )
        with session_file_lock(path):
            if path.exists():
                stat = path.stat()
                identity = (stat.st_ino, stat.st_size, stat.st_mtime_ns)
                cached = self._tails.get(event.session_id)
                if cached is not None and cached[:3] == identity:
                    expected = cached[3] + 1
                else:
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
            stat = path.stat()
            self._tails[event.session_id] = (
                stat.st_ino,
                stat.st_size,
                stat.st_mtime_ns,
                event.sequence,
            )

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
                event = self.decode_record(data)
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
