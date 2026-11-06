"""Text/JSON projection of received MCP content, retaining explicit unsupported markers."""

from dataclasses import dataclass

from jdagent.domain.json import JsonObject, JsonValue, require_array, require_object
from jdagent.domain.mcp import MCPCapabilityKind


@dataclass(frozen=True, slots=True)
class MCPContent:
    preview: JsonObject
    unsupported: tuple[str, ...]
    usable: bool


def project_content(raw: JsonObject, kind: MCPCapabilityKind) -> MCPContent:
    """Keep message roles as data; never turn Prompt messages into conversation facts."""
    unsupported: set[str] = set()
    usable = "structuredContent" in raw
    preview = {
        key: value for key, value in raw.items() if key not in {"content", "contents", "messages"}
    }

    def resource(value: JsonValue) -> JsonObject:
        nonlocal usable
        item = require_object(value, "MCP resource content")
        result = {key: item[key] for key in ("uri", "mimeType", "text") if key in item}
        if isinstance(item.get("text"), str):
            usable = True
        if "blob" in item:
            unsupported.add("binary_resource")
            result["unsupported"] = "binary_resource"
        return result

    def block(value: JsonValue) -> JsonObject:
        nonlocal usable
        item = require_object(value, "MCP content block")
        block_type = item.get("type")
        if block_type == "text":
            usable = True
            return {key: item[key] for key in ("type", "text", "annotations") if key in item}
        if block_type == "resource_link":
            usable = True
            return {
                key: item[key]
                for key in (
                    "type",
                    "uri",
                    "name",
                    "title",
                    "description",
                    "mimeType",
                    "size",
                    "annotations",
                )
                if key in item
            }
        if block_type == "resource":
            return {"type": "resource", "resource": resource(item.get("resource"))}
        label = block_type if isinstance(block_type, str) else "unknown"
        unsupported.add(label)
        return {"type": label, "unsupported": True}

    if kind == "resource":
        preview["contents"] = [
            resource(item) for item in require_array(raw.get("contents"), "resource contents")
        ]
    elif kind == "prompt":
        messages: list[JsonValue] = []
        for value in require_array(raw.get("messages"), "Prompt messages"):
            message = require_object(value, "Prompt message")
            messages.append({"role": message.get("role"), "content": block(message.get("content"))})
        preview["messages"] = messages
    else:
        preview["content"] = [
            block(item) for item in require_array(raw.get("content"), "tool content")
        ]
    return MCPContent(preview, tuple(sorted(unsupported)), usable)
