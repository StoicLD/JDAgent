# JDAgentProject Agent 地图

本仓库是需求、架构、决策、代码、测试、验收结果和项目当前状态的唯一权威来源。私有上下文仓库不是产品依赖。

## 启动顺序

1. 阅读本文件。
2. 如同级 `../JDAgentAgentContext/` 存在，阅读其 `AGENTS.md`、相关 `shared/` 规则和当前 Agent 自己的私有索引。
3. 阅读 [当前项目状态](docs/context/current.md) 和 [项目文档索引](docs/context/index.md)。
4. 只打开当前任务需要的权威文档。
5. 检查相关仓库的 branch、HEAD、remote 和未提交变更。

## 文档路由

- 需求与验收：[v0.1 Agent Runtime 需求](docs/requirements/v0.1-agent-runtime.md)
- 总体架构：[Agent Runtime 架构](docs/architecture/agent-runtime.md)
- 模型边界：[ModelPort 设计](docs/architecture/model-port.md)
- 决策记录：[ADR 目录](docs/decisions/)
- 实施阶段：[v0.1 实施计划](docs/plans/v0.1-implementation.md)
- Python 规范：[Python 工程规范](docs/development/python-style.md)
- 正式评审：`docs/reviews/`；目录在产生首份正式评审时创建。

## 工作规则

- 文档状态使用 `Draft`、`Proposed`、`Approved`、`Implemented`、`Superseded` 或 `Rejected`；Agent 不得自行批准提案。
- 讨论和诊断默认只读；实质性实现前必须有可验证的需求和验收标准。
- 代码变更遵循 Python 工程规范，并按风险覆盖正常路径、失败路径和回归测试。
- 不创建未经批准的框架、基础设施或空壳代码。
- 未经用户明确授权，不 commit、pull、push、merge、改写历史、切换分支或配置 remote。
- 不在仓库中保存密钥、Token、账号密码、支付信息或不必要的个人敏感数据。
