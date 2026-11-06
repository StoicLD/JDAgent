"""Narrow external previews without changing originals or execution facts."""

import hashlib
import json

from jdagent.domain.json import JsonObject, JsonValue, normalize_json, require_object


def source_preview(source: JsonValue) -> JsonValue:
    """Bound displayed identities; full origins remain in durable invocation facts."""
    if not isinstance(source, dict):
        return source
    projected: JsonObject = {}
    shortened = False
    for key, value in source.items():
        if isinstance(value, str) and len(encoded := value.encode("utf-8")) > 256:
            projected[f"{key}_prefix"] = encoded[:128].decode("utf-8", errors="ignore")
            projected[f"{key}_sha256"] = hashlib.sha256(encoded).hexdigest()
            shortened = True
        else:
            projected[key] = value
    if shortened:
        projected["details_in_history"] = True
    return projected


def narrow_preview(content: str) -> str:
    """Halve visible external text, keeping source and the verified original reference."""
    outcome = require_object(normalize_json(json.loads(content)), "MCP outcome")
    data = require_object(outcome.get("data"), "MCP result data")
    if not isinstance(data.get("original"), dict):
        return content
    if "preview" in data:
        text = json.dumps(data.pop("preview"), ensure_ascii=False, separators=(",", ":"))
        data["preview_text"] = text[: len(text) // 2]
        data["preview_format"] = "json_text_fragment"
        data["preview_complete"] = False
    elif isinstance(data.get("preview_text"), str):
        text = str(data["preview_text"])
        if not text:
            return content
        data["preview_text"] = text[: len(text) // 2]
        data["preview_complete"] = False
    elif isinstance(data.get("text"), str):
        text = str(data["text"])
        if not text:
            return content
        shortened = text[: len(text) // 2]
        data["text"] = shortened
        offset = data.get("offset")
        if isinstance(offset, int):
            data["next_offset"] = offset + len(shortened)
        data["complete"] = False
    else:
        return content
    return json.dumps(outcome, ensure_ascii=False)
