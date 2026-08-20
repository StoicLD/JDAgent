# JDAgentProject Agent 地图

本仓库是需求、架构、决策、代码、测试、验收结果和项目当前状态的唯一权威来源。私有上下文仓库不是产品依赖。

## 固定启动顺序

Codex、Cursor 和 Claude 可以直接从本仓库根目录启动，也可以从上级工作区根目录经 `AGENTS.md` 或 `CLAUDE.md` 路由到本仓库。进入本仓库启动链后，开始任何任务前依次执行：

1. 阅读本文件。
2. 如同级 `../JDAgentAgentContext/` 存在，阅读其 [Agent 地图](../JDAgentAgentContext/AGENTS.md)。
3. 阅读唯一的完整 [协作及目录组织指南](../JDAgentAgentContext/docs/bootstrap-guide.md)。
4. 阅读 Context 仓库中的 [共享协作规则](../JDAgentAgentContext/shared/collaboration-rules.md)、[用户偏好](../JDAgentAgentContext/shared/user-preferences.md) 和 [评审准则](../JDAgentAgentContext/shared/review-rubric.md)。
5. 只读取当前 Agent 自己的私有入口和当前恢复文件：
   - Codex：`../JDAgentAgentContext/codex/index.md`、`../JDAgentAgentContext/codex/current.md`
   - Cursor：`../JDAgentAgentContext/cursor/index.md`、`../JDAgentAgentContext/cursor/current.md`
   - Claude：`../JDAgentAgentContext/claude/CLAUDE.md`、`../JDAgentAgentContext/claude/index.md`、`../JDAgentAgentContext/claude/current.md`
6. 阅读 [当前项目状态](docs/context/current.md) 和 [项目文档索引](docs/context/index.md)。
7. 只打开当前任务需要的其他权威文档。
8. 检查相关仓库的 branch、HEAD、remote 和未提交变更。

如果 Context 仓库不存在，跳过第 2 至 5 步，以本仓库的产品事实继续只读恢复，并向用户报告私有上下文缺失。任一 Agent 都不得读取或修改其他 Agent 的私有目录。

## 文档路由

- 需求与验收：[v0.1 Agent Runtime 需求](docs/requirements/v0.1-agent-runtime.md)
- 当前需求与验收：[v0.2 CLI 体验需求](docs/requirements/v0.2-cli-experience.md)
- 总体架构：[Agent Runtime 架构](docs/architecture/agent-runtime.md)
- 当前 CLI 架构：[CLI 应用层架构](docs/architecture/cli-application.md)
- 核心运行契约：[Runtime 核心契约](docs/architecture/runtime-contracts.md)
- 模型边界：[ModelPort 设计](docs/architecture/model-port.md)
- 决策记录：[ADR 目录](docs/decisions/)
- 实施阶段：[v0.1 实施计划](docs/plans/v0.1-implementation.md)
- 当前实施阶段：[v0.2 CLI 实施计划](docs/plans/v0.2-cli-implementation.md)
- Python 规范：[Python 工程规范](docs/development/python-style.md)
- 正式评审：`docs/reviews/`；目录在产生首份正式评审时创建。

## 工作规则

- 文档状态使用 `Draft`、`Proposed`、`Approved`、`Implemented`、`Superseded` 或 `Rejected`；Agent 不得自行批准提案。
- 讨论和诊断默认只读；实质性实现前必须有可验证的需求和验收标准。
- 代码变更遵循 Python 工程规范，并按风险覆盖正常路径、失败路径和回归测试。
- 不创建未经批准的框架、基础设施或空壳代码。
- 未经用户明确授权，不 commit、pull、push、merge、改写历史、切换分支或配置 remote。
- 不在仓库中保存密钥、Token、账号密码、支付信息或不必要的个人敏感数据。
