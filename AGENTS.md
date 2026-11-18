# JDAgent Agent 地图

本文件是产品仓库的原生入口。**当前代码是产品行为的唯一 source of truth**；文档解释代码，
历史方案、审批与验收记录用于追溯。此规则来自所有者 2026-11-11 的文档整理要求。
产品文件与协作规则均维护在本仓库，外层目录只是启动路由。

## 固定启动顺序

1. 阅读[文档 Map](docs/map.md)。
2. 阅读[当前状态](docs/context/current.md)。
3. 阅读[协作与评审规则](docs/development/agent-collaboration.md)。
4. 按任务读取当前架构／模块及其代码；只在追溯理由或验证历史时进入归档、ADR 和评审。
5. 检查本仓库的 branch、HEAD、remote、工作树和未提交变更，再确定写入范围。

[旧索引](docs/context/index.md)仅保留兼容导航。当前文档与代码不一致时，以代码核对并修订文档，
不要为了迎合旧方案改变实现。无法从代码确定的运行质量标为未验证；归档不改变历史验收结果。

## 工作边界

- 新功能或行为变更需要明确范围和可验证的验收标准；现状文档整理不要求重新批准已有实现。
- 文档状态使用 `Draft`、`Proposed`、`Approved`、`Implemented`、`Superseded` 或 `Rejected`。
  说明实现存在不等于批准旧提案，也不等于工程／学习验收完成。
- 实质性模块遵循[交付与学习工作流](docs/development/module-delivery-workflow.md)；
  Python 变更遵循[工程规范](docs/development/python-style.md)，检查入口见[开发指南](docs/development/contributing.md)。
- Git 提交、推送、合并、历史改写、分支切换或 remote 修改须有当前适用的用户授权。
- 不覆盖用户的未提交修改；不在仓库保存凭据或不必要的敏感数据。
- 不把仓库外的私有 Context、缓存、聊天摘要或临时输出加入启动链。
- 入口缺失、Git 状态无法确认或事实仍无法核实时，停止受影响的写入，保留现场并报告证据。

## 按需规则

- 操作 GitHub Issues 前，读[任务跟踪约定](docs/agents/issue-tracker.md)。
- 开展领域建模或架构分析前，读[领域文档约定](docs/agents/domain.md)。
