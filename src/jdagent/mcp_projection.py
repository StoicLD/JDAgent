"""Narrow external previews without changing originals or execution facts."""

import json

from jdagent.domain.json import normalize_json, require_object


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
