# 2026-11-11 文档归档

状态：`Implemented`（归档整理）。

依据所有者“当前代码是唯一 source of truth”的要求，按 `e23604fcc4660fa393d65f07215d6ac220532e2f`
重新核对产品文档。下列 32 份材料保留为历史，退出当前实现的默认阅读链。

归档不表示原方案全部错误，也不代表未完成工作已验收。原审批状态、失败尝试、学习记录和授权时间线保留。
新增归档提示并调整相对链接；原始字节可在基准提交中按原路径读取。
[manifest.json](manifest.json) 保存原路径、归档路径和迁移前 SHA-256；历史评审指纹不重算。

当前入口：[架构总览](../../architecture/overview.md)、[使用教程](../../guides/usage.md)、
[模块地图](../../map.md)、[当前状态](../../context/current.md)、[验证说明](../../modules/verification.md)。

## 逐文件迁移

| 原路径（相对 docs） | 归档材料 | 归档原因／当前替代 |
| --- | --- | --- |
| `architecture/knowledge-contracts-v0.3.md` | [v0.3 知识 Port、Payload 与 Runtime Event v2 合同](architecture/knowledge-contracts-v0.3.md) | 阶段目标混有未实现或已变更细节；当前架构与模块说明替代 |
| `architecture/milvus-revision-saga-v0.3.md` | [v0.3 Milvus Collection、Revision Filter 与 Saga 合同](architecture/milvus-revision-saga-v0.3.md) | 阶段目标混有未实现或已变更细节；当前架构与模块说明替代 |
| `architecture/model-port.md` | [ModelPort 设计](architecture/model-port.md) | 阶段目标混有未实现或已变更细节；当前架构与模块说明替代 |
| `architecture/v0.3-knowledge-retrieval.md` | [v0.3 RAG 与来源可追踪知识检索高层设计](architecture/v0.3-knowledge-retrieval.md) | 阶段目标混有未实现或已变更细节；当前架构与模块说明替代 |
| `architecture/v0.5-context-memory.md` | [v0.5 上下文与记忆架构](architecture/v0.5-context-memory.md) | 阶段目标混有未实现或已变更细节；当前架构与模块说明替代 |
| `architecture/v0.6-langchain-langgraph.md` | [v0.6 LangChain 与 LangGraph 重构设计](architecture/v0.6-langchain-langgraph.md) | 阶段目标混有未实现或已变更细节；当前架构与模块说明替代 |
| `architecture/v0.7-mcp-client.md` | [v0.7 MCP Client：R1 运行时衔接设计](architecture/v0.7-mcp-client.md) | 阶段目标混有未实现或已变更细节；当前架构与模块说明替代 |
| `context/current.md` | [当前项目状态](context/current.md) | 设计讨论或旧状态快照；当前状态、术语和模块说明替代 |
| `context/v0.3-rag-glossary.md` | [v0.3 知识检索语境](context/v0.3-rag-glossary.md) | 设计讨论或旧状态快照；当前状态、术语和模块说明替代 |
| `context/v0.3-rag-illustrated-guide.md` | [用一份休假政策读懂 v0.3 RAG](context/v0.3-rag-illustrated-guide.md) | 设计讨论或旧状态快照；当前状态、术语和模块说明替代 |
| `context/v0.4-tools-glossary.md` | [v0.4 工具执行语境](context/v0.4-tools-glossary.md) | 设计讨论或旧状态快照；当前状态、术语和模块说明替代 |
| `context/v0.5-context-memory-design-discussion.md` | [v0.5 访谈决策与术语](context/v0.5-context-memory-design-discussion.md) | 设计讨论或旧状态快照；当前状态、术语和模块说明替代 |
| `development/v0.3-rag-manual-verification.md` | [v0.3 RAG 手动验证报告与操作指南](development/v0.3-rag-manual-verification.md) | 阶段验收、实验或一次性迁移材料；保留历史复现与追溯用途 |
| `development/v0.5-context-memory-validation.md` | [v0.5 验收与 Eval 方案](development/v0.5-context-memory-validation.md) | 阶段验收、实验或一次性迁移材料；保留历史复现与追溯用途 |
| `development/v0.5-session-reset.md` | [v0.5 旧会话一次性清理记录](development/v0.5-session-reset.md) | 阶段验收、实验或一次性迁移材料；保留历史复现与追溯用途 |
| `development/v0.6-acceptance-matrix.md` | [v0.6 验收映射与剩余工作](development/v0.6-acceptance-matrix.md) | 阶段验收、实验或一次性迁移材料；保留历史复现与追溯用途 |
| `development/v0.6-comparison-runbook.md` | [v0.6 六次对照实验操作说明](development/v0.6-comparison-runbook.md) | 阶段验收、实验或一次性迁移材料；保留历史复现与追溯用途 |
| `development/v0.6-comparison-template.md` | [v0.6 固定实验与比较记录模板](development/v0.6-comparison-template.md) | 阶段验收、实验或一次性迁移材料；保留历史复现与追溯用途 |
| `development/v0.7-acceptance-matrix.md` | [v0.7 MCP Client 验收映射](development/v0.7-acceptance-matrix.md) | 阶段验收、实验或一次性迁移材料；保留历史复现与追溯用途 |
| `map.md` | [JDAgent 文档 Map](map.md) | 旧导航快照；当前入口改为 docs/map.md |
| `plans/post-v0.2-learning-roadmap.md` | [v0.2 之后的模块学习路线](plans/post-v0.2-learning-roadmap.md) | 阶段安排和实施流水不再作为当前待办；未验证项进入当前状态／验证说明 |
| `plans/v0.3-rag-implementation.md` | [v0.3 RAG 与来源可追踪知识检索实施计划](plans/v0.3-rag-implementation.md) | 阶段安排和实施流水不再作为当前待办；未验证项进入当前状态／验证说明 |
| `plans/v0.3-rag-improvement-proposal.md` | [v0.3 RAG 工程补齐与检索质量改进方案](plans/v0.3-rag-improvement-proposal.md) | 阶段安排和实施流水不再作为当前待办；未验证项进入当前状态／验证说明 |
| `plans/v0.3-rag-p1-p2-appendix.md` | [v0.3 实施计划附页：P1/P2 默认项](plans/v0.3-rag-p1-p2-appendix.md) | 阶段安排和实施流水不再作为当前待办；未验证项进入当前状态／验证说明 |
| `plans/v0.4-implementation.md` | [v0.4 实现与验证记录](plans/v0.4-implementation.md) | 阶段安排和实施流水不再作为当前待办；未验证项进入当前状态／验证说明 |
| `plans/v0.5-context-memory-implementation.md` | [v0.5 实施计划](plans/v0.5-context-memory-implementation.md) | 阶段安排和实施流水不再作为当前待办；未验证项进入当前状态／验证说明 |
| `plans/v0.6-implementation.md` | [v0.6 实施进度与验证](plans/v0.6-implementation.md) | 阶段安排和实施流水不再作为当前待办；未验证项进入当前状态／验证说明 |
| `plans/v0.7-implementation.md` | [v0.7 MCP Client 实施记录](plans/v0.7-implementation.md) | 阶段安排和实施流水不再作为当前待办；未验证项进入当前状态／验证说明 |
| `requirements/v0.3-knowledge-retrieval.md` | [v0.3 RAG 与来源可追踪知识检索需求](requirements/v0.3-knowledge-retrieval.md) | 版本需求保留原批准状态；当前功能从代码与教程读取 |
| `requirements/v0.5-context-memory.md` | [v0.5 上下文、跨会话检索与长期记忆规格](requirements/v0.5-context-memory.md) | 版本需求保留原批准状态；当前功能从代码与教程读取 |
| `requirements/v0.7-mcp-client.md` | [v0.7 通用 MCP Client 规格](requirements/v0.7-mcp-client.md) | 版本需求保留原批准状态；当前功能从代码与教程读取 |
| `research/v0.6-langchain-langgraph.md` | [v0.6 LangChain 与 LangGraph 官方资料核对](research/v0.6-langchain-langgraph.md) | 设计时点的外部资料核对；当前框架行为以锁定依赖接线代码为准 |

正式评审与原始运行证据仍在 `docs/reviews/`；只修订受迁移影响的导航，不改结论。
ADR 和学习清单保留原目录与状态。早期 v0.1／v0.2／v0.4 归档见[总索引](../README.md)。
