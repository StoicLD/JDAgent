"""Logical request manifests and deterministic source fingerprints."""

import hashlib
import json
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import asdict
from datetime import datetime

from jdagent.domain.events import CompactionFailedPayload, RuntimeEvent
from jdagent.domain.json import (
    JsonObject,
    JsonValue,
    normalize_json,
    require_array,
    require_object,
    require_string,
)
from jdagent.domain.model import (
    MessageRole,
    ModelMessage,
    ModelRequest,
    ModelSettings,
    ModelToolDefinition,
    SystemPart,
)
from jdagent.domain.tools import ToolCall
from jdagent.knowledge.ptk import PreparedTurnKnowledge
from jdagent.ports import ContextObjects


def _json_default(value: object) -> str:
    if isinstance(value, datetime):
        return value.isoformat()
    raise TypeError(f"Unsupported archived value: {type(value).__name__}")


def encode_json(value: object) -> bytes:
    """Use one canonical representation for immutable context objects."""
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=_json_default,
        allow_nan=False,
    ).encode("utf-8")


def decode_object(content: bytes) -> JsonObject:
    return require_object(normalize_json(json.loads(content)), "context object")


def prefix_hash(events: Sequence[RuntimeEvent], through: int) -> str:
    records: list[object] = []
    for event in events:
        if event.sequence > through:
            continue
        record = asdict(event)
        if isinstance(event.payload, CompactionFailedPayload) and not event.payload.response_hashes:
            # Keep the original v0.5 schema-4 canonical form when the optional field is absent.
            record["payload"].pop("response_hashes", None)
        records.append(record)
    return hashlib.sha256(encode_json(records)).hexdigest()


async def archive_request(
    request: ModelRequest,
    objects: ContextObjects,
    knowledge: PreparedTurnKnowledge | None,
) -> str:
    """Do not create an owned copy of externally managed knowledge evidence."""
    systems: list[JsonValue] = []
    for part in request.system_parts:
        if part.source == "untrusted_evidence":
            if knowledge is None:
                raise ValueError("Knowledge evidence requires versioned source references")
            references: list[JsonValue] = [
                {
                    "evidence_id": item.evidence_id,
                    "reference": item.reference,
                    "knowledge_base_id": item.knowledge_base_id,
                    "source_id": item.source_id,
                    "source_version_id": item.source_version_id,
                    "snapshot_id": item.snapshot_id,
                    "locator": item.locator,
                    "locator_schema_version": item.locator_schema_version,
                    "content_hash": item.content_hash,
                    "ordinal": item.ordinal,
                    "kb_name": item.kb_name,
                    "source_name": item.source_name,
                    "token_estimate": item.token_estimate,
                }
                for item in knowledge.evidence
            ]
            systems.append(
                {
                    "kind": "knowledge_ref",
                    "render_version": 1,
                    "source": part.source,
                    "references": references,
                    "turn_token": knowledge.turn_token,
                    "outcome": knowledge.outcome.value,
                    "degradations": list(knowledge.degradations),
                }
            )
            continue
        systems.append(
            {
                "kind": "owned_object",
                "source": part.source,
                "hash": await objects.put(part.content.encode("utf-8")),
            }
        )
    messages: list[JsonValue] = []
    for message in request.messages:
        messages.append(
            {"kind": "owned_object", "hash": await objects.put(encode_json(asdict(message)))}
        )
    manifest: JsonObject = {
        "version": 1,
        "model": request.model,
        "systems": systems,
        "messages": messages,
        "tools": normalize_json([asdict(t) for t in request.tools]),
        "settings": normalize_json(asdict(request.settings)),
        "metadata": dict(request.metadata),
        "provider_options": request.provider_options,
    }
    return await objects.put(encode_json(manifest))


async def reconstruct_request(
    digest: str,
    objects: ContextObjects,
    *,
    resolve_knowledge: Callable[[JsonObject], Awaitable[str]] | None = None,
) -> ModelRequest:
    """Rebuild logical requests; deleted knowledge stays a missing external dependency.

    The knowledge owner verifies source lifecycle, version, locator and content hash before
    returning the exact rendered part. It must raise on a tombstone; this archive has no KB copy.
    """
    manifest = decode_object(await objects.get(digest))
    if manifest.get("version") != 1:
        raise ValueError("Unsupported request manifest version")
    systems: list[SystemPart] = []
    for entry in require_array(manifest.get("systems"), "systems"):
        item = require_object(entry, "system")
        if item.get("kind") == "knowledge_ref":
            if resolve_knowledge is None:
                raise ValueError("Missing knowledge dependency resolver; source may be tombstoned")
            content = await resolve_knowledge(item)
        elif item.get("kind") == "owned_object":
            content = (await objects.get(require_string(item, "hash"))).decode("utf-8")
        else:
            raise ValueError("Unknown request content ownership")
        systems.append(SystemPart(content, require_string(item, "source")))
    messages: list[ModelMessage] = []
    for entry in require_array(manifest.get("messages"), "messages"):
        reference = require_object(entry, "message reference")
        item = decode_object(await objects.get(require_string(reference, "hash")))
        calls: list[ToolCall] = []
        for value in require_array(item.get("tool_calls"), "tool_calls"):
            call = require_object(value, "call")
            calls.append(
                ToolCall(
                    require_string(call, "call_id"),
                    require_string(call, "name"),
                    require_object(call.get("arguments"), "arguments"),
                )
            )
        call_id, name = item.get("tool_call_id"), item.get("name")
        messages.append(
            ModelMessage(
                MessageRole(require_string(item, "role")),
                require_string(item, "content"),
                tuple(calls),
                call_id if isinstance(call_id, str) else None,
                name if isinstance(name, str) else None,
            )
        )
    definitions: list[ModelToolDefinition] = []
    for entry in require_array(manifest.get("tools"), "tools"):
        item = require_object(entry, "tool")
        definitions.append(
            ModelToolDefinition(
                require_string(item, "name"),
                require_string(item, "description"),
                require_object(item.get("input_schema"), "input_schema"),
            )
        )
    settings = require_object(manifest.get("settings"), "settings")
    temperature, output, timeout = (
        settings.get("temperature"),
        settings.get("max_output_tokens"),
        settings.get("timeout_seconds"),
    )
    if not isinstance(timeout, (int, float)):
        raise ValueError("Invalid request timeout")
    metadata = require_object(manifest.get("metadata"), "metadata")
    if not all(isinstance(value, str) for value in metadata.values()):
        raise ValueError("Invalid request metadata")
    return ModelRequest(
        require_string(manifest, "model"),
        tuple(messages),
        tuple(systems),
        tuple(definitions),
        ModelSettings(
            float(temperature) if isinstance(temperature, (int, float)) else None,
            output if isinstance(output, int) else None,
            float(timeout),
        ),
        {key: str(value) for key, value in metadata.items()},
        require_object(manifest.get("provider_options"), "provider_options"),
    )
