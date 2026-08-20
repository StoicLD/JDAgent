# Runtime 核心契约

状态：`Approved`

批准日期：2026-08-12

v0.2 契约补充批准日期：2026-08-20

## 目的与边界

本文固定 Agent Runtime 各模块之间必须共享的最小语义，避免 Agent Loop、CLI、存储和工具
各自发明状态。主体记录 v0.1 已实现合同；标明 v0.2 的小节是已批准、尚待实现的增量合同。
这里定义的是项目内部契约，不承诺成为永久公共 API。

Provider 协议细节仍属于各自 Adapter；JSONL、终端交互和文件系统实现也不得进入 Core。

## Turn 停止原因

`StopReason` 表示一次 Turn 为何停止，稳定值为：

- `completed`：模型正常完成且没有待执行 Tool Call。
- `cancelled`：用户或上层调用方取消。
- `model_error`：模型边界返回不可继续的错误。
- `context_limit`：ContextBuilder 在调用模型前判定超过硬限制。
- `limit_reached`：达到模型调用、工具调用或其他已配置上限。
- `session_error`：恢复或追加必要 Session 事件失败。
- `internal_error`：无法归入上述类别的 Runtime 缺陷。

工具失败、参数错误、权限拒绝和用户拒绝审批通常转换为 `ToolResult` 返回模型，不直接终止 Turn。只有它们导致 Runtime 无法继续，或触发明确上限时，才映射为停止原因。

`ResponseCompleted.finish_reason` 是 Provider 响应事实；`StopReason` 是 Runtime 对整个 Turn 的最终判断，两者不能共用同一个枚举。

## 分层错误分类

不建立一个覆盖所有层的全局 `ErrorCategory`。每个边界维护自己的稳定分类，再由 Turn Coordinator 决定是否以及如何映射为 `StopReason`。

### `ModelErrorCategory`

- `authentication`
- `permission`
- `rate_limit`
- `timeout`
- `connection`
- `context_length`
- `invalid_request`
- `invalid_response`
- `provider_internal`
- `cancelled`

### `ToolErrorCode`

- `unknown_tool`
- `invalid_arguments`
- `permission_denied`
- `approval_rejected`
- `path_outside_workspace`
- `timeout`
- `execution_failed`

### `SessionErrorCode`

- `not_found`
- `corrupt_event`
- `unsupported_schema`
- `append_failed`
- `read_failed`

错误对象可以携带安全的诊断信息、原始状态码和因果链，但不得包含 API Key、认证 Header、未经筛选的 Provider 正文或文件敏感内容。

## 核心协作契约

以下是设计层接口，具体签名在实现时以 Python `Protocol` 和 typed boundary model 表达：

```python
class SessionPort(Protocol):
    async def append(self, event: RuntimeEvent) -> None: ...
    async def read(self, session_id: str) -> AsyncIterator[RuntimeEvent]: ...


class ToolRuntimePort(Protocol):
    async def execute(self, call: ToolCall, context: ToolExecutionContext) -> ToolResult: ...


class ApprovalPort(Protocol):
    async def request(self, request: ApprovalRequest) -> ApprovalDecision: ...


class RuntimeEventSink(Protocol):
    async def emit(self, event: RuntimeEvent) -> None: ...
```

- `PermissionPolicy` 是纯决策服务：根据工具、参数、workspace 和风险返回 `ALLOW | ASK | DENY`，不读取终端。
- `ApprovalPort` 是交互边界：仅在 Policy 返回 `ASK` 时收集用户决定。CLI 是 v0.1 的首个 Adapter。
- `ContextBuilder` 是确定性应用服务：从 Session 投影、System Prompt 和 ToolDefinition 构造 `ModelRequest`，不是外部 I/O Port。
- `ToolRuntimePort` 封装查找、Schema 校验、权限、审批、超时、Handler 调用和 `ToolResult` 转换；Agent Loop 不得复制这些步骤。
- `RuntimeEventSink` 接收规范事件：恢复相关事件先通过 `SessionPort` 成功追加，再交给 Trace 等观察者；观察者不能回写或改写业务事实。
- M1 必须提供可工作的 Fake/InMemory Adapter，不使用只会抛出 `NotImplementedError` 的空实现来冻结架构。

### v0.2 ApprovalPort 增量合同

v0.1 `ApprovalDecision.APPROVE | REJECT` 只能表达单次决定。v0.2 将 ApprovalPort 的结果扩展为
结构化 `ApprovalOutcome`；具体 Python 类型在 M4 实现，但语义现在固定：

```python
class ApprovalOutcome:
    decision: ApprovalDecision
    granted_rule: SessionPermissionRule | None


class ApprovalPort(Protocol):
    async def request(self, request: ApprovalRequest) -> ApprovalOutcome: ...
```

- `REJECT` 时 `granted_rule` 必须为空，Handler 不执行。
- “本次允许”使用 `APPROVE + None`，只允许当前 Tool Call。
- “本 Session 允许”使用 `APPROVE + SessionPermissionRule`。
- Rule 至少包含稳定 `rule_id`、Session ID、工具名、`file | directory` 目标类型，以及由
  Tool preflight 和 `WorkspacePathResolver` 得出的规范 workspace-relative 目标。
- Terminal Adapter 不构造、匹配或保存 Rule；它只把用户选择返回 ApprovalPort Adapter。
- Permission 模块在应用 Rule 前重新解析目标并 fail closed；字符串前缀匹配仍然禁止。
- v0.2 用户级写权限上限只有 `ASK | DENY`。项目配置可以把 `ASK` 收紧为 `DENY`，不能配置
  `ALLOW`；Session Rule 只能把仍为 `ASK` 且目标匹配的调用转换为 `ALLOW`，永远不能覆盖
  `DENY`。
- Rule grant/revoke 是 Session 生命周期事实，必须持久化、可审计，并由 `/permissions` 查看和
  撤销。`/exit` 不结束 Session，因此恢复同一 Session 后 Rule 继续有效。
- Rule 不跨 Session、workspace 或用户级权限上限传播。具体 grant/revoke event payload 在 M4
  开工闸门固定；在此之前不得实现旁路白名单。

## Runtime Event Schema v1

`RuntimeEvent` 是 Session 与 Trace 共享的规范事件词汇。每个可持久化事件至少包含：

| 字段 | 含义 |
| --- | --- |
| `schema_version` | 固定为 `1` |
| `event_id` | 全局唯一事件 ID |
| `session_id` | 所属 Session |
| `turn_id` | 所属 Turn；Session 级事件也必须使用约定值或可空类型 |
| `sequence` | Session 内严格递增序号 |
| `event_type` | 稳定事件类型 |
| `timestamp` | UTC 时间戳；不参与业务排序 |
| `payload` | 与 `event_type` 对应的 typed payload |

v0.1 的恢复相关事件类型为：

- `session_started`
- `user_message`
- `assistant_message_completed`
- `tool_call_requested`
- `permission_requested`
- `permission_resolved`
- `tool_execution_started`
- `tool_execution_completed`
- `model_usage_recorded`
- `turn_completed`
- `turn_failed`

Session 只持久化恢复和审计所需的规范事件。高频 `TextDelta` 等流式展示事件可以只在进程内分发，不要求写入 JSONL；最终 assistant message 必须持久化。

Trace 消费同一条规范事件流并生成面向调试的投影，可以补充耗时和展示字段，但不得定义第二套业务状态或反向成为 Session 恢复依据。

## Workspace 路径解析

所有文件工具共用一个 `WorkspacePathResolver`：

1. 启动时将配置的 workspace 根目录解析为规范 real path。
2. 读取目标必须存在；解析目标 real path 后用操作系统原生路径包含关系判断是否位于根目录内。
3. 写入目标可以尚不存在；从最近的已存在父目录开始解析 real path，确认父目录位于根目录内，再逐段构造目标。
4. 拒绝 `..` 逃逸、绝对路径越界、符号链接或 junction 逃逸，以及无法安全解析的路径。
5. 禁止使用字符串前缀判断，例如 `candidate.startswith(workspace)`。

路径校验、Schema 校验、权限和审批必须全部发生在副作用之前。

## JSONL Adapter v1 语义

- 文件编码为 UTF-8；每行恰好一个完整 JSON `RuntimeEvent`，每个事件自带 `schema_version`。
- v0.1 只支持单进程内的并发协调。Adapter 对 append 使用进程内锁，一次写入完整的 UTF-8 行。
- append 只有在写入、flush 和 `fsync` 成功后才报告成功；失败映射为 `append_failed`。
- reader 逐行验证 JSON、Schema 版本、Session ID 和严格递增的 `sequence`。
- 最后一行不完整或无法解析时，返回带字节偏移的 `corrupt_event`；不得静默忽略、自动截断或自动修复。
- 非尾部坏行、未知 Schema 版本和顺序冲突均 fail closed。
- 多进程 writer、文件锁恢复、自动修复和事件迁移不属于 v0.1。

### v0.2 显式 Recovery 增量合同

v0.1 的普通 `SessionPort.read` 和 JSONL reader 继续严格 fail closed；不得因为 v0.2 而静默
忽略或自动截断损坏。v0.2 新增的 Recovery 是显式应用/存储 seam，只能在 Session 打开前运行：

```text
strict SessionPort.read
        ↑
validated repaired file
        ↑
explicit Session Recovery
```

物理恢复合同：

1. Recovery 在排除并发 writer 后读取原始 bytes，不复用“返回部分事件”的宽松 reader。
2. 只有“至少一个完整有效事件 + 唯一残缺最终记录”可以修复；完整前缀必须通过 JSON、Schema、
   Session ID 和严格 sequence 全量验证。
3. 文件从首条记录即残缺、非尾部坏行、未知 Schema、Session ID 冲突或 sequence 冲突全部
   `UNRECOVERABLE`，原文件不变。
4. 修复前把原始 bytes 写入同目录 sibling backup；名称包含 Session ID、UTC 时间和原文件
   SHA-256 前缀。backup 写入、flush、`fsync` 未全部成功时不得修改原文件。
5. 修复内容写入同目录临时文件，flush、`fsync` 后使用同卷原子替换；失败保留原文件和 backup，
   并返回稳定 Recovery 错误。
6. 修复必须返回结构化结果 `NO_REPAIR_NEEDED | REPAIRED_FINAL_PARTIAL_RECORD | UNRECOVERABLE`，
   包含安全字节偏移、backup 标识和诊断；不得输出记录正文。
7. 物理修复完成后仍须执行逻辑 Turn 分类；`REPAIRED` 不等于 Session 可以自动继续。

逻辑恢复继续遵循 [CLI 应用层架构](cli-application.md) 的物理×逻辑分类。写风险工具存在
`tool_execution_started` 而缺少对应 `tool_execution_completed` 时，副作用状态不确定，必须
阻止自动继续。异常恢复 Session 的持久表示和 ContextBuilder 投影在 M3 开工闸门单独批准。

## 契约级验收

- Agent Loop 只依赖 `ModelPort`、`ToolRuntimePort` 和明确的运行状态，不直接处理权限、文件路径或 JSONL。
- ContextBuilder 可以仅凭同一事件序列生成确定一致的 `ModelRequest`。
- CLI 审批 Adapter 可替换为测试 Fake，而不修改 Policy 或 Tool Runtime。
- Session 与 Trace 对同一 Turn 的事件类型和关联 ID 一致，Trace 不成为第二事实源。
- 越界路径、未批准写入、模型超时、工具超时、取消和损坏 JSONL 都有命名测试覆盖。
