# 项目文档索引

状态：`Implemented`

## 恢复入口

- [当前项目状态](current.md)
- [仓库 Agent 地图](../../AGENTS.md)
- [项目概览](../../README.md)

## 需求与计划

- [v0.1 Agent Runtime 需求](../requirements/v0.1-agent-runtime.md) — `Approved`
- [v0.1 实施计划](../plans/v0.1-implementation.md) — `Approved`，全部里程碑与学习出口已完成
- [v0.2 CLI 体验需求](../requirements/v0.2-cli-experience.md) — `Approved`
- [v0.2 CLI 实施计划](../plans/v0.2-cli-implementation.md) — `Implemented`，工程与学习门禁
  全部关闭
- [v0.2 之后的模块学习路线](../plans/post-v0.2-learning-roadmap.md) — `Approved`，批准
  v0.3–v0.5 的学习方向与顺序，不替代各版本的具体需求和设计审批

## 架构

- [Agent Runtime 架构](../architecture/agent-runtime.md) — `Approved`
- [Runtime 核心契约](../architecture/runtime-contracts.md) — `Approved`
- [ModelPort 设计](../architecture/model-port.md) — `Approved`
- [CLI 应用层架构](../architecture/cli-application.md) — `Approved`
- [v0.2 Session 生命周期事实](../architecture/session-lifecycle-v0.2.md) — `Approved`
- [v0.2 Session 异常恢复表示](../architecture/session-recovery-v0.2.md) — `Approved`
- [v0.2 Session Permission Rule 合同](../architecture/permission-rules-v0.2.md) — `Approved`
- [v0.2 Windows 终端附录](../architecture/windows-terminal-v0.2.md) — `Approved`

## 已批准决策

- [ADR-0001：Python 作为唯一 Agent Core 语言](../decisions/ADR-0001-python-core.md)
- [ADR-0002：模型无关 ModelPort](../decisions/ADR-0002-model-port.md)
- [ADR-0003：通用 Runtime 与求职领域分离](../decisions/ADR-0003-runtime-domain-separation.md)
- [ADR-0004：CLI 应用层与终端 Adapter](../decisions/ADR-0004-cli-application-layer.md)

## 开发规范

- [Python 工程规范](../development/python-style.md) — `Approved`
- [模块交付与学习工作流](../development/module-delivery-workflow.md) — `Approved`，定义模块边界、
  AI/学习者角色、设计包、Eval、工程验收和学习验收

## 评审与证据

- [v0.1 实现审查](../reviews/v0.1-implementation-review.md) — `pass_with_findings`；离线实现
  无未处理代码 finding；后续真实 DeepSeek 与 M10b 验收已通过并记录于当前状态
- [v0.1 完成度审计](../reviews/v0.1-completion-audit.md) — `Implemented`，按 FR、验收标准、
  里程碑与 M10b 逐项核验
- [v0.1 评审内容快照](../reviews/v0.1-review-snapshot.sha256) — 不 commit 约束下的逐文件
  SHA-256 固定范围
- [v0.2 CLI 独立审查处置](../reviews/REV-20260820-001-v0.2-cli-plan-disposition.md) —
  F01–F12 的接受、调整后接受与里程碑前置门禁
- [v0.2 实现审查](../reviews/v0.2-implementation-review.md) — Standards 与 Spec 复核均为
  0 Blocker、0 High；含离线、Live、PowerShell 与安装验证证据

## 学习出口

- [v0.1 人工学习检查表](../learning/v0.1-learning-checklist.md) — `Implemented`，2026-08-20 完成
- [v0.2 CLI 人工学习检查表](../learning/v0.2-learning-checklist.md) — `Implemented`，项目所有者
  于 2026-08-24 按学习范围裁定关闭；原题保留为可选复习材料
- [模块学习检查表模板](../learning/module-learning-checklist-template.md) — `Approved`，每个后续
  实质性模块设计完成时实例化并填充具体机制、数据流和故障实验
