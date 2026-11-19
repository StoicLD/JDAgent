# JDAgent 文档地图

状态：`Implemented`（当前实现导航）。v0.8 执行路径核对日期为 2026-11-19，改造前基准为 `9d7cbdc`。

**当前代码是产品行为的唯一事实来源。** 本目录解释代码；历史方案、计划和验收用于追溯，
不能据此声称代码已有某项能力。发现说明与实现不一致时，核对代码并修订说明。

## 按目的阅读

| 目的 | 入口 |
| --- | --- |
| 初次安装和体验 | [README](../README.md) → [使用教程](guides/usage.md) |
| 配置模型、权限、数据与预算 | [配置参考](reference/configuration.md) |
| 接入 MCP | [配置与操作](guides/mcp.md) → [实现说明](modules/mcp.md) |
| 导入知识材料 | [知识库教程](guides/knowledge.md)，先阅读可用范围 |
| 理解整体设计 | [架构总览](architecture/overview.md) → 下表的模块 |
| 修改代码 | [当前状态](context/current.md) → [协作规则](development/agent-collaboration.md) → [开发指南](development/contributing.md) |
| 查验测试与限制 | [验证与验收](modules/verification.md)及模块内的代码／测试链接 |
| 查历史取舍、审批和版本证据 | [ADR 索引](decisions/README.md)、[归档索引](archive/README.md) |
| 查看 v0.8 实现范围 | [LangGraph 单执行引擎的改造规格与实施计划](plans/v0.8-langgraph-turn-runner.md)、[验证记录](development/v0.8-langgraph-verification.md)及 [GitHub Issue #3](https://github.com/StoicLD/JDAgent/issues/3) |

## 当前模块

| 模块 | 回答的问题 |
| --- | --- |
| [运行时](modules/runtime.md) | Coordinator 与图执行器怎样执行一轮任务 |
| [模型](modules/models.md)／[ModelPort 合同](architecture/model-port.md) | 模型请求、流和错误怎样跨越适配器边界 |
| [上下文](modules/context.md) | 请求如何构造、计量、归档和压缩 |
| [历史与记忆](modules/memory.md) | 如何检索旧会话、保存记忆版本、整理和计费 |
| [工具与权限](modules/tools.md) | 本地操作、目录规则、审批及 MCP 调用如何受控 |
| [会话与事件](modules/sessions.md) | 哪些数据持久保存，异常与未知副作用如何恢复 |
| [CLI](modules/cli.md) | 交互、单次执行、控制命令和配置怎样接线 |
| [知识库](modules/knowledge.md) | Catalog、内容对象、索引、检索和引用如何配合 |
| [MCP](modules/mcp.md) | 连接、目录、动态定义、原文、Task 与来源如何管理 |

[术语表](reference/glossary.md)解释当前代码中的对象。模块文档的 `Implemented` 表示说明已有实现，
不表示整版工程或人工学习验收完成。

## 工程与学习

- [Python 规范](development/python-style.md)、[交付与学习工作流](development/module-delivery-workflow.md)
- [任务跟踪](agents/issue-tracker.md)、[领域文档约定](agents/domain.md)
- 学习清单：[v0.1](learning/v0.1-learning-checklist.md)、[v0.2](learning/v0.2-learning-checklist.md)、
  [v0.3](learning/v0.3-rag-learning-checklist.md)、[v0.4](learning/v0.4-tools-learning-checklist.md)、
  [v0.5](learning/v0.5-context-memory-learning-checklist.md)、[v0.6](learning/v0.6-langchain-langgraph-learning-checklist.md)、
  [v0.7](learning/v0.7-mcp-client-learning-checklist.md)。它们记录阶段练习与完成情况，不是当前功能清单。

## 历史边界

2026-11-11 已将版本方案、需求、计划、阶段验收映射和状态流水移入[本次归档](archive/2026-11-11/README.md)。
原审批状态、失败和人工未完成项保留。正式评审与证据仍在 `docs/reviews/`，迁移只调整导航链接。
需要复现固定评审字节时使用报告指定的 Git revision。

[context/index.md](context/index.md)仅保留旧启动路径的兼容导航。
