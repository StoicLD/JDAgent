# 当前项目状态

状态：`Draft`

## 当前阶段

v0.1 需求已批准。架构、Runtime 核心契约和实施顺序已根据独立审查意见修订，当前等待用户最终批准。当前没有产品实现。

## 已批准事实

- `JDAgentProject` 是产品与工程事实的唯一权威仓库。
- `JDAgentAgentContext` 是同级私有 Agent Context 仓库，不是产品构建、测试或运行依赖。
- 第一阶段建设领域无关的通用 Agent Runtime；第二阶段通过求职领域包进行特化。
- Agent Core 使用 Python 3.11+，不维护第二套 TypeScript Core。
- Agent Core 依赖模型无关的 `ModelPort`；首个真实 Provider Adapter 对接 DeepSeek。
- 项目权威文档使用中文；未来代码标识符、公共 API、Docstring 和错误类型使用英文。
- [v0.1 Agent Runtime 需求](../requirements/v0.1-agent-runtime.md) 已于 2026-08-09 获得用户批准。

## 当前提案

- [Agent Runtime 架构](../architecture/agent-runtime.md)
- [Runtime 核心契约](../architecture/runtime-contracts.md)
- [ModelPort 设计](../architecture/model-port.md)
- [v0.1 实施计划](../plans/v0.1-implementation.md)
- [Python 工程规范](../development/python-style.md)

上述文档仍是 `Proposed`，不能解释为已经批准或实现。

## 进行中的工作

- 审阅修订后的 Agent Runtime 架构、Runtime 核心契约、ModelPort 设计、Python 工程规范与实施顺序。
- 本轮修订已将 ContextBuilder、Session 投影、Tool Runtime 和 Permission 前移，并将 Agent Loop 与 Fake CLI 垂直切片后移。

## 已知阻塞项

- Agent Runtime 架构、Runtime 核心契约、ModelPort 详细设计、Python 工程规范和实施计划尚未获得用户最终批准。
- Python 包名和项目发布名须在 M0 前决定；首个 DeepSeek 模型配置只须在 M9 前决定，不阻塞离线 Runtime。

## 下一步

1. 用户审阅架构、Runtime 核心契约、ModelPort、Python 工程规范和修订后的实施计划。
2. 上述提案批准后确认包名并创建 Python 工程配置，按计划从领域类型、Ports 和可工作 Fake 开始。
3. 在产生首个可验证行为后更新本文件，不用实现计划代替真实状态。

## 恢复入口

- [项目文档索引](index.md)
- [仓库 Agent 地图](../../AGENTS.md)
