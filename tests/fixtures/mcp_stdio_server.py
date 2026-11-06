"""Independent, stdlib-only MCP wire peer for transport fault injection."""

import json
import os
import subprocess
import sys
import time


def main() -> None:
    version = sys.argv[1]
    list_calls = 0
    task: dict[str, object] = {
        "taskId": "stdio-task",
        "status": "working",
        "ttlMs": None,
        "createdAt": "2026-11-06T00:00:00Z",
        "lastUpdatedAt": "2026-11-06T00:00:00Z",
    }
    for line in sys.stdin.buffer:
        message = json.loads(line)
        if "id" not in message:
            continue
        method = message["method"]
        result: dict[str, object]
        if method == "server/discover" and version == "2025-11-25":
            print(
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": message["id"],
                        "error": {
                            "code": -32601,
                            "message": "Method not found",
                        },
                    }
                ),
                flush=True,
            )
            continue
        if method == "server/discover":
            result = {
                "supportedVersions": [version],
                "capabilities": {
                    "tools": {},
                    "resources": {},
                    "prompts": {},
                    "extensions": {"io.modelcontextprotocol/tasks": {}},
                },
            }
        elif method == "initialize":
            result = {
                "protocolVersion": version,
                "capabilities": {"tools": {}, "resources": {}, "prompts": {}},
                "serverInfo": {"name": "independent-fixture", "version": "1"},
            }
        elif method == "tools/list":
            list_calls += 1
            cursor = message.get("params", {}).get("cursor")
            if cursor == "remote-error":
                print(
                    json.dumps(
                        {
                            "jsonrpc": "2.0",
                            "id": message["id"],
                            "error": {"code": -32001, "message": "fixture-secret-error-text"},
                        }
                    ),
                    flush=True,
                )
                continue
            if cursor == "hang":
                time.sleep(60)
            result = {
                "tools": [],
                "resultType": "complete",
                "cacheScope": "private",
                "ttlMs": 0,
                "_meta": {"fixturePid": os.getpid(), "listCalls": list_calls},
            }
            if cursor == "oversized":
                result["tools"] = [
                    {
                        "name": "large",
                        "description": "x" * (1 << 20),
                        "inputSchema": {"type": "object"},
                    }
                ]
        elif method == "resources/list":
            result = {
                "resources": [{"name": "manual", "uri": "docs://manual"}],
                "resultType": "complete",
                "cacheScope": "private",
                "ttlMs": 0,
            }
        elif method == "resources/templates/list":
            result = {
                "resourceTemplates": [{"name": "page", "uriTemplate": "docs://{page}"}],
                "resultType": "complete",
                "cacheScope": "private",
                "ttlMs": 0,
            }
        elif method == "prompts/list":
            result = {
                "prompts": [
                    {"name": "explain", "arguments": [{"name": "topic", "required": True}]}
                ],
                "resultType": "complete",
                "cacheScope": "private",
                "ttlMs": 0,
            }
        elif method == "tools/call":
            name = message["params"]["name"]
            if name == "unterminated":
                sys.stdout.buffer.write(b"x" * 4096)
                sys.stdout.buffer.flush()
                continue
            result = {"resultType": "complete", "content": [{"type": "text", "text": "ok"}]}
            if name == "task":
                assert version == "2026-07-28"
                capabilities = message["params"]["_meta"][
                    "io.modelcontextprotocol/clientCapabilities"
                ]
                assert "io.modelcontextprotocol/tasks" in capabilities["extensions"]
                result = {**task, "resultType": "task"}
            if name == "spawn_child":
                child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
                result["content"] = [{"type": "text", "text": f"{os.getpid()},{child.pid}"}]
            if name == "hang":
                time.sleep(60)
            if name == "sized":
                body = json.dumps(
                    {"jsonrpc": "2.0", "id": message["id"], "result": result}
                ).encode()
                size = message["params"]["arguments"]["bytes"]
                sys.stdout.buffer.write(body + b" " * (size - len(body)) + b"\n")
                sys.stdout.buffer.flush()
                continue
        elif method == "tasks/get":
            assert message["params"]["taskId"] == "stdio-task"
            result = {
                **task,
                "resultType": "complete",
                "status": "completed",
                "result": {
                    "resultType": "complete",
                    "content": [{"type": "text", "text": "done"}],
                },
            }
        else:
            result = {}
        print(json.dumps({"jsonrpc": "2.0", "id": message["id"], "result": result}), flush=True)


if __name__ == "__main__":
    main()
