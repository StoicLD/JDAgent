# 模型接入

状态：`Implemented`（实现说明）

模型接入把统一请求转换为 Provider 协议，再将响应转换为统一事件。
它不执行工具，也不决定整个 Turn 是否完成。实现基准见[Map](../map.md)。

## 接口与数据流

`ModelPort` 暴露 `capabilities` 和 `stream(ModelRequest)`。
请求包含模型名、系统指令、消息、工具定义、生成设置、关联元数据与 Provider 选项。
`ModelCapabilities` 声明是否支持工具调用、流式输出和上下文窗口等能力；
ContextBuilder 在发请求前验证必要能力。

响应通过异步事件流返回：`TextDelta` 用于文本增量，`ToolCallDelta`／`ToolCallCompleted`
用于工具参数流与完整调用，`UsageReported` 记录用量，`ResponseCompleted` 标记响应结束，
`ModelError` 表达模型边界错误。Loop 消费这些类型，Provider 原始 JSON 不进入 Loop。

## 现有适配器

| 实现 | 用途与行为 |
| --- | --- |
| `DeepSeekModelPort` | 使用 httpx 请求 `/chat/completions`，解析 SSE、拼装工具参数、映射 HTTP／流错误 |
| `FakeModelPort` | 按预设脚本输出事件，支持延迟与重复脚本，为测试提供确定性响应 |

默认配置为 DeepSeek、`deepseek-v4-flash`、`https://api.deepseek.com`；
这是仓库当前默认值，不表示对外部服务最新型号的判断。
DeepSeek 请求关闭 thinking，当前统一事件模型不承载 reasoning 内容。
普通 CLI 的 Fake 默认返回 `Fake response`；复杂工具轨迹由测试注入脚本。

模型超时默认 30 秒，由请求设置和 Loop 控制；具体值可经 CLI／配置覆盖。
HTTP 错误映射为认证、权限、限流、连接、超时等类别，Loop 再映射整个 Turn 的停止原因。
流结束与工具调用的一致性由 Loop 复核，见[运行时](runtime.md)。

## 依赖与扩展

组合根选择真实或 Fake Adapter，并注入 Loop。更换 Provider 需要实现同一端口并验证事件合同，
不要求修改工具 Handler 或 Session 存储格式。当前只有 DeepSeek 一个真实生成模型适配器。
RAG 的 Embedding 是独立接口与配置，不能用生成模型接入成功推断检索配置也已完成。

Key 的解析顺序见[CLI 与配置](cli.md)，解析后的值仅传入 Adapter。
`RuntimeComposition.aclose()` 负责关闭真实模型客户端。

## 代码与验证入口

- [模型类型](../../src/jdagent/domain/model.py)、[ModelPort](../../src/jdagent/ports.py)
- [DeepSeek Adapter](../../src/jdagent/adapters/deepseek.py)、[Fake Adapter](../../src/jdagent/adapters/fake.py)
- [协议测试](../../tests/test_deepseek_adapter.py)、[端口替身测试](../../tests/test_contract_adapters.py)、
  [真实模型测试](../../tests/integration/test_deepseek_live.py)
- [ModelPort 设计](../architecture/model-port.md)、[ADR-0002](../decisions/ADR-0002-model-port.md)

历史真实模型结果与当前门禁分别见[验证与验收](verification.md)。
