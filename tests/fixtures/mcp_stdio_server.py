"""Independent, stdlib-only MCP wire peer for transport fault injection."""

import json
import os
import subprocess
import sys
import time
from pathlib import Path


def main() -> None:
    version = sys.argv[1]
    list_calls = 0
    tool_calls: dict[str, int] = {}
    task_polls = 0
    task_mode: str | None = None
    task_cancelled = False
    task_created = False
    task_signal: str | None = None
    capabilities: dict[str, object] = {"tools": {}, "resources": {}, "prompts": {}}
    if len(sys.argv) > 2 and sys.argv[2] == "prompts_only":
        capabilities = {"prompts": {}}
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
        if method in {"tasks/get", "tasks/cancel"} and not task_created:
            print(
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": message["id"],
                        "error": {
                            "code": -32602,
                            "message": "Task no longer exists in this process",
                        },
                    }
                ),
                flush=True,
            )
            continue
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
                    **capabilities,
                    "extensions": {"io.modelcontextprotocol/tasks": {}},
                },
            }
        elif method == "initialize":
            result = {
                "protocolVersion": version,
                "capabilities": capabilities,
                "serverInfo": {"name": "independent-fixture", "version": "1"},
            }
        elif method == "tools/list":
            assert "tools" in capabilities
            list_calls += 1
            if len(sys.argv) > 2 and sys.argv[2] == "catalog_once" and list_calls > 2:
                raise RuntimeError("Directory was fetched again without reconnecting")
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
            if len(sys.argv) > 2 and sys.argv[2].startswith("catalog"):
                if cursor == "page2":
                    reader: dict[str, object] = {
                        "name": "read",
                        "description": "Read repository files 读取仓库文件",
                        "inputSchema": {"type": "object"},
                    }
                    if sys.argv[2] == "catalog_output_array":
                        reader["outputSchema"] = {"type": "array", "items": {"type": "integer"}}
                    if sys.argv[2] == "catalog_large_schema":
                        reader["inputSchema"] = {
                            "type": "object",
                            "properties": {"path": {"type": "string", "description": "p" * 6200}},
                        }
                    result["tools"] = [reader]
                else:
                    catalog_tools: list[dict[str, object]] = [
                        {
                            "name": "search",
                            "description": "Search repository code 搜索仓库代码",
                            "inputSchema": {"type": "object"},
                        },
                        {
                            "name": "write",
                            "description": "Write repository files",
                            "inputSchema": {"type": "object"},
                        },
                    ]
                    result["tools"] = catalog_tools
                    result["nextCursor"] = "page2"
                    if sys.argv[2] == "catalog_invalid":
                        catalog_tools.extend(
                            [
                                {
                                    "name": "broken",
                                    "description": "invalid schema",
                                    "inputSchema": {
                                        "type": "object",
                                        "properties": {"x": {"type": "nonsense"}},
                                    },
                                },
                                {
                                    "name": "external_ref",
                                    "description": "external schema",
                                    "inputSchema": {
                                        "type": "object",
                                        "$ref": "https://must-not-fetch.invalid/schema",
                                    },
                                },
                                {
                                    "name": "output_external",
                                    "inputSchema": {"type": "object"},
                                    "outputSchema": {
                                        "$ref": "https://must-not-fetch.invalid/result"
                                    },
                                },
                            ]
                        )
                        catalog_tools.extend(
                            [
                                {"name": "ambiguous", "inputSchema": {"type": "object"}},
                                {
                                    "name": "ambiguous",
                                    "inputSchema": {"type": "object", "required": ["x"]},
                                },
                            ]
                        )
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
        elif method == "resources/read":
            uri = message["params"]["uri"]
            contents: list[dict[str, object]] = [{"uri": uri, "text": "Remote file material"}]
            result = {
                "resultType": "complete",
                "contents": contents,
            }
            if uri.endswith("/binary"):
                result["contents"] = [
                    {"uri": uri, "blob": "AAE=", "mimeType": "application/octet-stream"}
                ]
            elif uri.endswith("/mixed"):
                contents.append(
                    {"uri": uri + "/binary", "blob": "AAE=", "mimeType": "application/octet-stream"}
                )
        elif method == "prompts/get":
            assert message["params"]["name"] == "explain"
            topic = message["params"]["arguments"]["topic"]
            result = {
                "resultType": "complete",
                "messages": [
                    {
                        "role": "user",
                        "content": {
                            "type": "text",
                            "text": "remote-only " * 2000
                            if topic == "fixture_large"
                            else "External template for: " + topic,
                        },
                    }
                ],
            }
        elif method == "tools/call":
            name = message["params"]["name"]
            tool_calls[name] = tool_calls.get(name, 0) + 1
            timeout_mode = message["params"].get("arguments", {}).get("fixture_timeout")
            if timeout_mode == "stall":
                time.sleep(3)
            if timeout_mode == "always" or timeout_mode == "twice" and tool_calls[name] <= 2:
                print(
                    json.dumps(
                        {
                            "jsonrpc": "2.0",
                            "id": message["id"],
                            "error": {"code": -32001, "message": "private-server-diagnostic"},
                        }
                    ),
                    flush=True,
                )
                continue
            if name == "unterminated":
                sys.stdout.buffer.write(b"x" * 4096)
                sys.stdout.buffer.flush()
                continue
            result = {"resultType": "complete", "content": [{"type": "text", "text": "ok"}]}
            if timeout_mode is not None:
                result["content"] = [{"type": "text", "text": f"attempts={tool_calls[name]}"}]
            if timeout_mode == "text_error":
                result["isError"] = True
                result["content"] = [{"type": "text", "text": "timeout; attempts=1"}]
            if name == "read" and "fixture_structured" in message["params"].get("arguments", {}):
                result["structuredContent"] = message["params"]["arguments"]["fixture_structured"]
                result["content"] = []
            if name == "read" and "fixture_blocks" in message["params"].get("arguments", {}):
                result["content"] = message["params"]["arguments"]["fixture_blocks"]
            if name == "read" and message["params"].get("arguments", {}).get("fixture_error"):
                result["isError"] = True
            if name == "read" and "fixture_size" in message["params"].get("arguments", {}):
                result["content"] = [
                    {"type": "text", "text": "z" * message["params"]["arguments"]["fixture_size"]}
                ]
            if name == "task" or message["params"].get("arguments", {}).get("fixture_task"):
                task_created = True
                task_mode = message["params"].get("arguments", {}).get("fixture_task")
                task_signal = message["params"].get("arguments", {}).get("fixture_signal")
                task_polls = 0
                task_cancelled = False
                assert version == "2026-07-28"
                client_capabilities = message["params"]["_meta"][
                    "io.modelcontextprotocol/clientCapabilities"
                ]
                assert "io.modelcontextprotocol/tasks" in client_capabilities["extensions"]
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
            task_polls += 1
            if (
                task_mode in {"query_hang", "cancel_race", "cancel_ignored", "cancel_stalls"}
                and not task_cancelled
            ):
                if task_signal is not None:
                    Path(task_signal).touch()
                continue
            if task_mode in {"query_timeout", "query_exhausted"} and (
                task_polls <= 2 or task_mode == "query_exhausted"
            ):
                print(
                    json.dumps(
                        {
                            "jsonrpc": "2.0",
                            "id": message["id"],
                            "error": {"code": -32001, "message": "private-task-query-error"},
                        }
                    ),
                    flush=True,
                )
                continue
            result = {
                **task,
                "resultType": "complete",
                "status": "completed",
                "result": {
                    "resultType": "complete",
                    "content": [{"type": "text", "text": "done"}],
                },
            }
            if task_mode is not None:
                result["result"] = {
                    "resultType": "complete",
                    "content": [
                        {
                            "type": "text",
                            "text": f"done; calls={tool_calls['read']}; polls={task_polls}",
                        }
                    ],
                }
                if task_cancelled:
                    if task_mode == "cancel_race":
                        result["status"] = "completed"
                    else:
                        result.pop("result")
                        result["status"] = (
                            "working" if task_mode == "cancel_ignored" else "cancelled"
                        )
                elif task_polls == 1:
                    result.pop("result")
                    result["status"] = (
                        "input_required" if task_mode == "input_required" else "working"
                    )
                    result["pollIntervalMs"] = 1
                    if task_mode == "input_required":
                        result["inputRequests"] = {"ask": {"method": "roots/list"}}
                elif task_mode == "failed":
                    result.pop("result")
                    result["status"] = "failed"
                    result["error"] = {"code": -32001, "message": "private-task-failure"}
                elif task_mode == "cancelled":
                    result.pop("result")
                    result["status"] = "cancelled"
                elif task_mode == "business_error":
                    result["result"] = {
                        "resultType": "complete",
                        "content": [{"type": "text", "text": "business failed; calls=1"}],
                        "isError": True,
                    }
        elif method == "tasks/cancel":
            assert message["params"]["taskId"] == "stdio-task"
            if task_mode == "cancel_stalls":
                continue
            task_cancelled = True
            result = {"resultType": "complete"}
        else:
            result = {}
        print(json.dumps({"jsonrpc": "2.0", "id": message["id"], "result": result}), flush=True)


if __name__ == "__main__":
    main()
