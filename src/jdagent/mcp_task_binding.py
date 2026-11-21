"""保存远程任务与本地引用的绑定，并核对绑定和会话登记记录是否一致。"""

import json
from dataclasses import asdict, dataclass, field

from jdagent.domain.events import MCPTaskRegisteredPayload, RuntimeEvent
from jdagent.domain.json import normalize_json, require_object, require_string
from jdagent.domain.mcp import MCPCapability, MCPTask


@dataclass(frozen=True, slots=True)
class MCPTaskBinding:
    """将私有远端任务标识绑定到原始调用、工作区和连接身份，使重启后的管理操作仍可核验目标。"""

    task: MCPTask = field(repr=False)
    identity_digest: str = field(repr=False)
    session_id: str
    turn_id: str
    call_id: str
    workspace_identity: str
    task_ref: str
    definition: MCPCapability

    def to_bytes(self) -> bytes:
        """按 v0.7 的控制对象格式序列化任务关联，保证既有对象仍能逐字节核验。"""
        control = {**asdict(self), "server_id": self.definition.server_id}
        return json.dumps(control, ensure_ascii=False, sort_keys=True).encode("utf-8")

    @classmethod
    def from_bytes(
        cls,
        data: bytes,
        *,
        registration: RuntimeEvent,
        session_id: str,
        workspace_identity: str,
    ) -> "MCPTaskBinding":
        """读取不可变控制对象并核对登记事件中的身份，只有两者一致才恢复远端任务标识。"""
        record = registration.payload
        if not isinstance(record, MCPTaskRegisteredPayload):
            raise ValueError("Task registration is required")
        control = require_object(normalize_json(json.loads(data.decode("utf-8"))), "Task binding")
        if (
            control.get("session_id") != session_id
            or registration.session_id != session_id
            or control.get("task_ref") != record.task_ref
            or control.get("turn_id") != registration.turn_id
            or control.get("call_id") != record.call_id
            or control.get("server_id") != record.server_id
            or control.get("workspace_identity") != workspace_identity
        ):
            raise ValueError("Task control binding is inconsistent")
        definition = require_object(control.get("definition"), "Task definition")
        if (
            definition.get("kind") != "tool"
            or definition.get("server_id") != record.server_id
            or definition.get("name") != record.raw_name
            or definition.get("generation") != record.generation
        ):
            raise ValueError("Task definition binding is inconsistent")
        entry = MCPCapability(
            record.server_id,
            record.generation,
            "tool",
            record.raw_name,
            require_string(definition, "description"),
            require_string(definition, "definition_json"),
        )
        raw_task = require_object(control.get("task"), "Task handle")
        task = MCPTask.from_data(
            {
                "taskId": raw_task.get("task_id"),
                "status": raw_task.get("status"),
                "createdAt": raw_task.get("created_at"),
                "ttlMs": raw_task.get("ttl_ms"),
                "pollIntervalMs": raw_task.get("poll_interval_ms"),
            }
        )
        return cls(
            task,
            require_string(control, "identity_digest"),
            session_id,
            require_string(control, "turn_id"),
            record.call_id,
            workspace_identity,
            record.task_ref,
            entry,
        )
