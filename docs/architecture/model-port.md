# ModelPort 当前合同

状态：`Implemented`（实现说明）。v0.8 执行路径核对日期为 2026-11-19，改造前基准为 `9d7cbdc`。

`ModelPort` 将模型协议与 Runner 隔开。接口在 [ports.py](../../src/jdagent/ports.py)，
字段与事件在 [domain/model.py](../../src/jdagent/domain/model.py)。它不执行工具、审批、保存会话或决定任务完成。
原始设计已[归档](../archive/2026-11-11/architecture/model-port.md)，本文仅说明当前合同。

```python
class ModelPort(Protocol):
    @property
    def capabilities(self) -> ModelCapabilities: ...

    def stream(self, request: ModelRequest) -> AsyncIterator[ModelEvent]: ...
```

## 请求与能力

| ModelRequest 字段 | 作用 |
| --- | --- |
| `model` | 本次 Provider 模型标识 |
| `messages` | 项目 ModelMessage：user、assistant、tool，含工具调用与结果配对 |
| `system_parts` | 系统指令及来源 |
| `tools` | 本请求可见的名称、描述与输入 Schema |
| `settings` | 仅有 temperature、max_output_tokens、timeout_seconds |
| `metadata` | 请求的关联信息 |
| `provider_options` | 适配器允许的扩展字段 |

ModelCapabilities 包含 streaming、tool_calls、parallel_tool_calls、structured_output、reasoning、context_window。
能力声明不改变 Runner 的顺序执行策略。DeepSeek 的 context_window 声明为 None；实际预算使用
[model_profiles.py](../../src/jdagent/model_profiles.py)或用户显式窗口配置。

## 流式返回与完成

TextDelta 展示文本；ToolCallDelta 只展示／组装片段；ToolCallCompleted 提供完整调用候选。
UsageReported 记录用量，ResponseCompleted 记录 Provider finish_reason，ModelFailed 表达分类错误。
Runner 等完整流通过一致性检查后才执行工具：工具响应必须以 `tool_calls` 结束，纯文本以 `stop` 结束。
Runner 还检查历史调用 ID，适配器只处理单次响应。Provider finish_reason 与 Turn StopReason 是不同合同。

调用方通过 [closing_model_stream](../../src/jdagent/model_stream.py)等待迭代器关闭；
取消、提前返回和协议错误都需要关闭流后再释放模型槽。

## 当前适配器

- [DeepSeekModelPort](../../src/jdagent/adapters/deepseek.py)：httpx + SSE，请求 `/chat/completions`。
- [LangChainModelPort](../../src/jdagent/adapters/langchain_model.py)：ChatDeepSeek，转换 SDK 消息／chunk，关闭 SDK 重试与云端追踪。
- [FakeModelPort](../../src/jdagent/adapters/fake.py)：确定性事件脚本，普通 CLI 返回固定文本。

当前真实生成 Provider 只有 DeepSeek。两种适配器都读取每份请求中的模型、输出上限和超时。
thinking 固定关闭，不能通过扩展启用；原生适配器允许的 deepseek 扩展包括 tool_choice、top_p、
reasoning_effort、response_format。不能由字段名字推断 reasoning 历史已支持。

密钥由组合根解析后传入适配器。错误按 authentication、permission、rate_limit、timeout、connection、
context_length、invalid_request、invalid_response、provider_internal、cancelled 分类；
错误分类本身不自动授权重新执行整个 Turn。

合同验证见 [DeepSeek 测试](../../tests/test_deepseek_adapter.py)、[LangChain 测试](../../tests/test_langchain_model.py)、
[模型流生命周期](../../tests/test_model_stream_lifecycle.py)、[双适配器接线](../../tests/test_v06_composition.py)。
