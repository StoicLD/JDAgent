# JDAgent Agent 地图

本文件是 Codex 与 Cursor 从产品 Git 仓库根启动时的原生入口。本仓库同时承载需求、架构、
决策、代码、测试、验收证据、协作规则和项目当前状态，是唯一权威事实来源。

## 固定启动顺序

无论直接从本仓库启动，还是由外层本地工作区路由进入，开始任务前依次执行：

1. 阅读共同的[项目文档索引](docs/context/index.md)。
2. 阅读[当前项目状态](docs/context/current.md)。
3. 阅读[Agent 协作与评审规则](docs/development/agent-collaboration.md)。
4. 只打开索引中与当前任务有关的需求、架构、计划、决策、规范和验证证据。
5. 检查本仓库的 branch、HEAD、remote、工作树和未提交变更，再判断允许的写入范围。

不得把仓库外的同级 Agent Context、工具私有目录、聊天摘要、缓存或临时输出加入启动链，
也不得把它们当作项目事实或正式交接接口。

## 最小工作边界

- 文档状态只使用 `Draft`、`Proposed`、`Approved`、`Implemented`、`Superseded` 或
  `Rejected`；Agent 不得自行批准提案。
- 讨论、诊断和评审默认只读；实质性实现前必须有可验证的需求和验收标准。
- v0.2 之后的实质性模块遵循[模块交付与学习工作流](docs/development/module-delivery-workflow.md)；
  Python 变更遵循[Python 工程规范](docs/development/python-style.md)。
- 未经用户明确授权，不 commit、pull、push、merge、改写历史、切换分支或配置 remote。
- 不在仓库中保存密钥、Token、账号密码、支付信息或不必要的个人敏感数据。
- 若入口链接缺失、权威事实冲突或 Git 状态无法确认，停止写入，保留现场并报告准确证据。
