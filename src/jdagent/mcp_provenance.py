"""记录 MCP 原始材料的来源，并将来源关联到使用了这些材料的派生内容。"""

from collections.abc import Iterable

from jdagent.domain.events import (
    AssistantMessageCompletedPayload,
    MCPPromptPreparedPayload,
    ModelRequestPreparedPayload,
    RuntimeEvent,
    ToolExecutionCompletedPayload,
)
from jdagent.domain.json import JsonObject


def external_provenance(events: Iterable[RuntimeEvent]) -> dict[int, JsonObject]:
    """按事件序号标记 MCP 来源及其可能影响的后续助手内容，独立的用户陈述仍按自身来源处理。"""
    references: dict[int, JsonObject] = {}
    external_seen = False
    for event in events:
        payload = event.payload
        source: JsonObject = {"kind": "mcp_derived"}
        original: JsonObject | None = None
        if isinstance(payload, ModelRequestPreparedPayload) and payload.mcp_context:
            source = {
                "kind": "mcp_derived_request",
                "request_id": payload.request_id,
                "request_hash": payload.object_hash,
            }
            external_seen = True
        elif isinstance(payload, MCPPromptPreparedPayload):
            actual = payload.outcome.data.get("source")
            if isinstance(actual, dict):
                source = actual
            stored = payload.outcome.data.get("original")
            if isinstance(stored, dict):
                original = stored
            external_seen = True
        elif isinstance(payload, ToolExecutionCompletedPayload):
            result = payload.result
            if not (result.metadata.get("tool_origin") or result.metadata.get("derived_from_mcp")):
                continue
            origin = result.metadata.get("tool_origin")
            if isinstance(origin, dict):
                source = origin
            if result.outcome is not None and isinstance(origin, dict):
                actual = result.outcome.data.get("source")
                if isinstance(actual, dict) and actual.get("kind") in {"mcp", "mcp_control"}:
                    source = actual
                stored = result.outcome.data.get("original")
                if isinstance(stored, dict):
                    original = stored
            external_seen = True
        elif not (external_seen and isinstance(payload, AssistantMessageCompletedPayload)):
            continue
        reference: JsonObject = {
            "session_id": event.session_id,
            "event_id": event.event_id,
            "sequence": event.sequence,
            "source": source,
        }
        if original is not None:
            reference["original"] = original
        references[event.sequence] = reference
    return references
