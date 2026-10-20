# 上下文构造

状态：`Implemented`（实现说明）

`ContextBuilder` 将会话事实转换成一次模型请求，并在调用前控制上下文大小。
它不保存第二份聊天历史。实现基准见[Map](../map.md)。

## 从事件到请求

`project_messages()` 将用户消息、完整 assistant 消息、工具执行结果和恢复快照投影为
Provider 无关的 `ModelMessage`。工具调用 ID 保持配对；结构化 `ToolOutcome`
序列化成工具消息内容，旧字符串结果路径仍用于兼容测试。

`build()` 在投影上加入系统指令、当前可用工具的输入 Schema、模型设置和关联元数据。
工具范围由组合根按本轮 `ToolCapability` 过滤，执行层仍再次校验。
如果本轮已准备知识，Builder 将证据作为 `untrusted_evidence` 系统片段注入；
本轮所有模型调用复用同一份知识，见[知识模块](knowledge.md)。

Citation repair 使用 `allow_tools=False`，并追加修复指令。它沿用上下文预算，
不能通过修复调用绕过窗口限制。

## 完整轮次滑动窗口

正常组合根启用滑动窗口。配置默认请求窗口为 64,000，输出预留为 4,096。
估算以 UTF-8 字节数、每条消息开销和输出预留计算，是保守估计，不是真实 tokenizer 计数。
同时存在配置窗口与模型能力窗口时，取较小限制。

超限时依次移除最旧的完整历史 Turn。恢复快照中的历史也按 USER 轮次裁剪，
`protected_tail_messages` 指定的恢复尾部保留。当前 Turn、系统指令、工具定义、
本轮证据与输出预留不通过旧轮次裁剪移除；仍超限则报 `ContextLimitError`。

裁剪仅作用于请求投影，原始 Session 事件保持不变。
省略标识随请求元数据返回，Loop 写入 `context_window_selected` 事件供追踪。

## 当前范围

目前已实现确定性投影、完整旧轮次裁剪和硬限制；没有摘要压缩、长期记忆提取或记忆检索。
v0.5 Memory、v0.6 Context / Compaction 的顺序见[后续路线](../plans/post-v0.2-learning-roadmap.md)。
窗口裁剪不会保证当前长任务一定能继续，也不会自动保留已被裁剪历史中的每项业务约束。

## 代码与验证入口

- [ContextBuilder 与消息投影](../../src/jdagent/context.py)
- [组合根的窗口设置](../../src/jdagent/composition.py)、[默认配置](../../src/jdagent/configuration.py)
- [上下文测试](../../tests/test_context_builder.py)、[完整轮次与快照回归](../../tests/test_v04_contracts.py)
- [v0.4 合同细化](../plans/v0.4-implementation.md)、[快照裁剪评审处置 P02](../reviews/v0.4-implementation-review.md)
