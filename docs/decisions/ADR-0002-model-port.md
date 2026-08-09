# ADR-0002：模型无关 ModelPort

状态：`Approved`

日期：2026-08-09

## 背景

项目首先接入 DeepSeek，但学习目标包括理解不同模型协议、流式输出、Function Calling、错误和能力差异。若 Agent Core 直接依赖某个 SDK，后续比较模型或增加 Provider 会污染核心循环。

## 决策

- Agent Core 只依赖项目定义的 `ModelPort`、`ModelRequest`、`ModelEvent`、`ModelCapabilities` 和稳定错误类别。
- 首个真实实现为 `DeepSeekModelPort`；测试实现为 `FakeModelPort`。
- Provider SDK 类型、原始 Chunk 和异常只能存在于对应 Adapter。
- Provider 特性通过能力声明和受控命名空间表达，不降低为不可扩展的最低公共能力。

## 备选方案

- Core 直接调用 DeepSeek/OpenAI 兼容客户端：初始代码更少，但协议和错误会进入 Agent Loop。
- 先使用通用 Agent 框架的模型抽象：开发更快，但隐藏本项目要学习的核心机制。

## 后果

- Core 可以用 Fake Model 做确定性测试，并在不修改循环的情况下增加 Provider。
- 项目必须维护自己的规范化类型和协议转换测试。
- `ModelPort` 不能执行工具、保存会话或决定任务是否完成。

## 复审条件

当两个真实 Provider 暴露出无法由能力声明或受控扩展表达的根本协议差异时，根据实验结果复审接口。

