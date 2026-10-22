# JDAgent 文档 Map

状态：`Implemented`（文档整理）

实现核对日期：2026-10-20；设计导航更新：2026-10-22。
实现基准：`12196d6895f97fdc140c6f46aee34ce90aabe4c8`。
下表描述该基准的现有代码；v0.5 设计单独列出，验收结论按原始证据记录。

## 从哪里开始

- 了解产品与启动方式：[README](../README.md)。
- 恢复工作：[当前状态](context/current.md) → [协作规则](development/agent-collaboration.md) → 本次相关模块。
- 理解现有实现：按下表进入模块；每篇列出职责、执行过程、限制和代码／测试入口。
- 查设计理由或历史承诺：先看有效 ADR，再看模块关联的设计与[归档索引](archive/README.md)。

## 当前实现

| 模块 | 负责回答的问题 |
| --- | --- |
| [运行时与主循环](modules/runtime.md) | 一轮任务怎么开始、调用模型和工具、结束；采用什么设计模式 |
| [模型接入](modules/models.md) | ModelPort 如何隔离 Provider，流式文本和工具调用如何转换 |
| [上下文构造](modules/context.md) | 事件如何变成消息，知识如何注入，窗口如何裁剪 |
| [工具与权限](modules/tools.md) | 八工具、审批、精确编辑、Windows 命令和输出日志如何工作 |
| [会话与事件](modules/sessions.md) | 什么是持久事实，如何保存、恢复和处理未知副作用 |
| [CLI 与配置](modules/cli.md) | 交互／Headless 入口、命令、配置优先级和数据位置 |
| [知识摄取与检索](modules/knowledge.md) | Catalog、Store、Index、检索和引用如何衔接，哪些能力仍有缺口 |
| [验证与验收](modules/verification.md) | 已有测试覆盖、最近证据、失败项、学习出口和复现入口 |

文档状态 `Implemented` 表示现状说明已落地，不等同于模块工程或学习验收通过。
代码事实发生变化时更新对应模块；新的测试结果进入评审记录，再更新验收摘要。

## 当前仍需使用的设计和计划

以下材料继续承担设计约束、未完成范围或授权记录的职责。`Proposed` 保持待批准状态；
阅读实现情况优先使用模块说明，不能仅凭设计中的“必须”判断功能已经存在。

| 用途 | 文档 |
| --- | --- |
| 模型边界的设计理由与合同 | [ModelPort 设计](architecture/model-port.md) |
| RAG 已批准目标和阶段范围 | [高层设计](architecture/v0.3-knowledge-retrieval.md)、[分阶段计划](plans/v0.3-rag-implementation.md)、[P1/P2 附页](plans/v0.3-rag-p1-p2-appendix.md) |
| RAG 尚待批准的详细规格 | [需求与验收](requirements/v0.3-knowledge-retrieval.md)、[知识 Port 与事件 v2 提案](architecture/knowledge-contracts-v0.3.md)、[Milvus 与 Saga 提案](architecture/milvus-revision-saga-v0.3.md) |
| RAG 工程收尾 | [改进方案](plans/v0.3-rag-improvement-proposal.md)、[人工验证操作报告](development/v0.3-rag-manual-verification.md) |
| v0.4 实施授权与合同细化 | [实现与验证记录](plans/v0.4-implementation.md) |
| v0.5 已确认产品行为 | [上下文、跨会话与记忆规格](requirements/v0.5-context-memory.md)、[访谈与术语](context/v0.5-context-memory-design-discussion.md) |
| v0.5 工程细化提案 | [架构](architecture/v0.5-context-memory.md)、[四阶段计划](plans/v0.5-context-memory-implementation.md)、[验收与 Eval](development/v0.5-context-memory-validation.md) |
| v0.5 R1 审查处置 | [Cursor 原始审查与逐项处置](reviews/REV-20261021-001-v0.5-context-memory-design-review.md)；R1 工程细化仍为 Proposed，待复审 |
| v0.5 旧会话过渡 | [一次性清理记录](development/v0.5-session-reset.md) |
| 后续学习模块顺序 | [v0.4 工具 → v0.5 Context 与 Memory](plans/post-v0.2-learning-roadmap.md) |

## 术语与学习资料

- RAG：[词汇表](context/v0.3-rag-glossary.md)、[休假政策图解](context/v0.3-rag-illustrated-guide.md)。
- 工具：[设计阶段词汇表](context/v0.4-tools-glossary.md)（`Proposed`）；当前工具行为见工具模块。
- 学习出口：[v0.1](learning/v0.1-learning-checklist.md)、[v0.2](learning/v0.2-learning-checklist.md)、
  [v0.3](learning/v0.3-rag-learning-checklist.md)、[v0.4](learning/v0.4-tools-learning-checklist.md)、
  [v0.5](learning/v0.5-context-memory-learning-checklist.md)（待执行）。
- 后续模块：[交付与学习工作流](development/module-delivery-workflow.md)、
  [学习检查表模板](learning/module-learning-checklist-template.md)。

## 有效决策

- [ADR-0001 Python Core](decisions/ADR-0001-python-core.md)
- [ADR-0002 ModelPort](decisions/ADR-0002-model-port.md)
- [ADR-0003 Runtime 与求职领域分离](decisions/ADR-0003-runtime-domain-separation.md)
- [ADR-0004 CLI 应用层](decisions/ADR-0004-cli-application-layer.md)
- [ADR-0005 产品仓库作为唯一启动事实源](decisions/ADR-0005-agent-context-free-startup.md)
- [ADR-0006 Milvus Standalone](decisions/ADR-0006-milvus-standalone-knowledge-index.md)

待评审决策：[ADR-0007 原始历史、上下文投影与记忆版本分离](decisions/ADR-0007-context-projection-memory.md)（`Proposed`）。

## 工程规范

- [Agent 入口](../AGENTS.md)、[协作与评审规则](development/agent-collaboration.md)
- [Python 工程规范](development/python-style.md)
- [任务跟踪约定](agents/issue-tracker.md)、[领域文档约定](agents/domain.md)

## 验收原始记录

| 范围 | 证据入口 |
| --- | --- |
| v0.1 | [实现审查](reviews/v0.1-implementation-review.md)、[完成度审计](reviews/v0.1-completion-audit.md)、[评审文件快照](reviews/v0.1-review-snapshot.sha256) |
| v0.2 | [计划评审处置](reviews/REV-20260820-001-v0.2-cli-plan-disposition.md)、[实现审查](reviews/v0.2-implementation-review.md) |
| v0.3 | [高层设计评审处置](reviews/REV-20260829-001-v0.3-rag-design-disposition.md)、[P1 实现评审处置](reviews/REV-20260830-001-v0.3-p1-rag-disposition.md)、[2026-09-05 实现审查与修复](reviews/REV-20260905-001-v0.3-implementation-audit.md) |
| v0.4 | [设计评审原文](reviews/REV-20260923-001-v0.4-tool-execution-design-review.md)、[实施审查与处置](reviews/v0.4-implementation-review.md)、[三次 Live 结果](reviews/v0.4-live-verification.md) |
| v0.5 | [设计初审与 R1 逐项处置](reviews/REV-20261021-001-v0.5-context-memory-design-review.md)、[R1 文件指纹](reviews/REV-20261021-001-v0.5-design-revision.sha256) |
| Eval 夹具 | [v0.3 Gold](../testdata/v0.3-gold/DATASET.md)、[v0.4 固定工具任务](../testdata/v0.4-tools/DATASET.md) |

## 实现参考
本项目的参考实现为codex和Dify，其中codex为JDAgent大部分模块的实现参考，Dify主要参考RAG部分的实现。参考目录并不意味着我们的项目是完全一致的复刻，而是作为设计灵感的来源，具体复杂问题的处理的参考，
比如codex可能某个模块已经有成熟的实现了，那么我们的项目在设计对应模块的时候也可以进行参考。
本地codex项目目录：
E:\Projects\Codex_Projects_Overall\OpenSourceAgentAnalysis\codex

本地Dify项目目录：
E:\Projects\Codex_Projects_Overall\OpenSourceAgentAnalysis\dify

## 历史归档

[归档索引](archive/README.md)按版本列出旧设计、已完成计划及迁移原因。
旧入口 [context/index.md](context/index.md)仅提供到本 Map 的兼容导航，不再维护第二份文档清单。
