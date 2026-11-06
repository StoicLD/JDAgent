"""Parse explicit Prompt commands shared by interactive and one-shot inputs."""

import re
import shlex

from jdagent.domain.json import JsonObject


def parse_mcp_prompt(text: str) -> JsonObject | None:
    """Retain the original trigger separately; only explicit commands fetch material."""
    if re.match(r"^/mcp\s+prompt(?:\s|$)", text.strip()) is None:
        return None
    parts = shlex.split(text)
    usage = "Usage: /mcp prompt <server> <name> [key=value ...]"
    if len(parts) < 4:
        raise ValueError(usage)
    arguments: JsonObject = {}
    for item in parts[4:]:
        key, separator, value = item.partition("=")
        if not key or not separator or key in arguments:
            raise ValueError(usage)
        arguments[key] = value
    return {"server_id": parts[2], "name": parts[3], "arguments": arguments}
