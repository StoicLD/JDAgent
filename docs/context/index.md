# 项目文档索引

状态：`Draft`

## 恢复入口

- [当前项目状态](current.md)
- [仓库 Agent 地图](../../AGENTS.md)
- [项目概览](../../README.md)

## 需求与计划

- [v0.1 Agent Runtime 需求](../requirements/v0.1-agent-runtime.md) — `Approved`
- [v0.1 实施计划](../plans/v0.1-implementation.md) — `Approved`，离线范围已实现

## 架构

- [Agent Runtime 架构](../architecture/agent-runtime.md) — `Approved`
- [Runtime 核心契约](../architecture/runtime-contracts.md) — `Approved`
- [ModelPort 设计](../architecture/model-port.md) — `Approved`

## 已批准决策

- [ADR-0001：Python 作为唯一 Agent Core 语言](../decisions/ADR-0001-python-core.md)
- [ADR-0002：模型无关 ModelPort](../decisions/ADR-0002-model-port.md)
- [ADR-0003：通用 Runtime 与求职领域分离](../decisions/ADR-0003-runtime-domain-separation.md)

## 开发规范

- [Python 工程规范](../development/python-style.md) — `Approved`

## 评审与证据

- [v0.1 实现审查](../reviews/v0.1-implementation-review.md) — `pass_with_findings`；离线实现
  无未处理代码 finding，仍缺真实 DeepSeek 验收与 M10b 人工学习出口
- [v0.1 完成度审计](../reviews/v0.1-completion-audit.md) — 按 FR、验收标准与里程碑逐项核验
- [v0.1 评审内容快照](../reviews/v0.1-review-snapshot.sha256) — 不 commit 约束下的逐文件
  SHA-256 固定范围

## 学习出口

- [v0.1 人工学习检查表](../learning/v0.1-learning-checklist.md) — `Ready`，待学习者本人完成
