"""Host-owned source references for MCP evidence and inseparable derived material."""

from collections.abc import Iterable

from jdagent.domain.events import (
    AssistantMessageCompletedPayload,
    MCPPromptPreparedPayload,
    RuntimeEvent,
    ToolExecutionCompletedPayload,
)
from jdagent.domain.json import JsonObject


def external_provenance(events: Iterable[RuntimeEvent]) -> dict[int, JsonObject]:
    """Keep independent user statements separate; later assistant prose may be mixed."""
    references: dict[int, JsonObject] = {}
    external_seen = False
    for event in events:
        payload = event.payload
        source: JsonObject = {"kind": "mcp_derived"}
        original: JsonObject | None = None
        if isinstance(payload, MCPPromptPreparedPayload):
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
