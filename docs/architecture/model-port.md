# ModelPort 设计

状态：`Proposed`

## 目的

`ModelPort` 是 Agent Core 使用模型能力的唯一接口。Core 只理解项目定义的请求、事件、能力和错误；DeepSeek、OpenAI、Anthropic、本地模型和 Fake Model 通过各自 Adapter 实现相同契约。

模型无关不等于抹平模型差异。通用能力进入稳定领域类型，Provider 特性通过能力声明和受控扩展表达，不能把原始 SDK 对象泄漏给 Core。

## 概念接口

```python
class ModelPort(Protocol):
    @property
    def capabilities(self) -> ModelCapabilities: ...

    def stream(self, request: ModelRequest) -> AsyncIterator[ModelEvent]: ...
```

`stream` 返回异步事件流，调用者使用 `async for` 消费。实现可以是包含 `yield` 的异步生成器；接口不负责重试整个 Agent Turn。

## 统一请求

`ModelRequest` 至少包含：

- `model`：逻辑模型选择，由 Adapter 解析为 Provider 模型标识。
- `system_parts`：分段系统指令，保留来源和顺序。
- `messages`：项目标准消息，不包含 Provider SDK 类型。
- `tools`：经过 Tool Registry 导出的稳定工具 Schema。
- `settings`：温度、最大输出、停止选项等已批准的通用设置。
- `metadata`：Session ID、Turn ID 和 Trace 关联信息，不发送秘密。
- `provider_options`：显式命名空间下的受控 Provider 扩展；Core 不读取其内容。

## 能力声明

`ModelCapabilities` 描述：

- `streaming`
- `tool_calls`
- `parallel_tool_calls`
- `structured_output`
- `reasoning`
- `context_window`

请求依赖不支持的能力时应在调用前产生明确错误，不静默降级。v0.1 不因 Provider 声明支持并行工具调用而并行执行工具。

## 事件模型

`ModelEvent` 是带类型标识的联合，至少覆盖：

- `TextDelta`：可展示的增量文本。
- `ToolCallDelta`：流式工具名称或参数片段，只用于组装，不能执行。
- `ToolCallCompleted`：具有稳定 call ID、工具名和完整参数的可执行候选。
- `UsageReported`：输入、输出及 Provider 可用的缓存或推理用量。
- `ResponseCompleted`：包含稳定 `finish_reason`。
- `ModelFailed`：包含分类错误和可安全记录的诊断信息。

只有 `ToolCallCompleted` 可以进入 Tool Runtime。重复或冲突的 call ID 必须作为协议错误处理。

## 错误分类

稳定错误类别包括：

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

Adapter 保存可用于诊断的 Provider 状态码和请求 ID，但不得把 API Key、认证 Header 或未经筛选的响应正文写入错误、事件或日志。

## DeepSeek Adapter

- 首选 DeepSeek 官方兼容 API，不在 Core 中依赖 OpenAI SDK 对象。
- 将标准消息、工具 Schema 和设置翻译成 DeepSeek 请求，将流式 Chunk 重建为统一事件。
- DeepSeek thinking、strict schema 等特性只能通过能力声明或 `provider_options.deepseek` 暴露。
- v0.1 不把 Beta strict mode 作为工具参数安全性的唯一保证；本地 Tool Runtime 始终独立校验 Schema。
- 重试仅覆盖明确可重试、且尚未产生不可安全重放副作用的 Provider 调用；详细重试策略在实现前另行批准。

## Fake Model

`FakeModelPort` 接收预设事件脚本，用于确定性覆盖文本、工具调用、多个工具调用、错误、超时和无限循环。测试不得通过 `if test_mode` 修改 Agent Core 行为。

## 非职责

`ModelPort` 不执行工具、不审批权限、不保存 Session、不压缩上下文、不检索记忆，也不决定 Agent 是否完成任务。

