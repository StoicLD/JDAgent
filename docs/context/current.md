# 当前项目状态

状态：`Implemented`（代码现状摘要）。更新：2026-11-19。

本页记录基于 `9d7cbdc` 开发的 v0.8 单引擎改造；具体实现从[架构总览](../architecture/overview.md)和[模块地图](../map.md)进入。
历史批准与实施流水已移至[整理前快照](../archive/2026-11-11/context/current.md)。

## 当前可运行内容

- Python 包版本 `0.5.0`；Session 事件 schema `4`，默认目录 `sessions-v5`；Headless JSON schema `2`。
- 默认 DeepSeek、`deepseek-v4-flash`、LangGraph 调度和原生 DeepSeek 适配器。可选择 LangChain 接入 DeepSeek。
- 本地工具、权限审批、持久会话、上下文压缩、历史与长期记忆均已接入正常 CLI。
- MCP 已接入工具、Resources、Prompts、双传输、检索和缓存、长调用及原 Task 管理。
- 知识库有管理、摄取、索引与引用实现；正常 CLI 的真实 RAG 配置链仍有缺口。

## 影响使用的限制

| 范围 | 当前边界／待验证内容 |
| --- | --- |
| 平台 | 文件写入和命令进程管理使用 Windows API；不能声明完整跨平台支持 |
| 执行 | 工具顺序执行；没有多 Agent、并行 Task、通用任务验收器或持久图恢复 |
| 次数配置 | 普通 CLI 的 RuntimeOptions 覆盖 TOML 调用／失败次数，实际为 16／32／2；见[配置参考](../reference/configuration.md#顶层字段) |
| 数据 | 旧 Session 不自动迁移；未知写入／命令结果不自动重放；备份须包含对象与父会话 |
| 知识库 | 默认对话不读取管理 CLI 的本地索引；Profile／凭据配置、恢复、物理删除与来源核验仍有缺口，见[知识模块](../modules/knowledge.md) |
| 质量 | 历史完整套件存在 RAG Recall 失败；真实服务、语义质量与学习出口不能从代码存在推断为通过 |
| MCP | 只启用用户配置名单；Task 顺序等待，重开后显式管理；不能保证远端取消立即停止工作 |

## 验证与遗留事项

v0.8 的全量基线、候选实现、安装与回退见[工程验证](../development/v0.8-langgraph-verification.md)。

更早的完整离线记录为 2026-11-06、`f747299`：934 passed、1 failed、14 skipped。
失败为 RAG 词项 Recall `0.8125 < 0.85`。此后到 `c412174` 的 MCP 修复有定向回归和独立复核，
`e23604f` 更新验收文档。该次文档检查见[文档核对记录](../development/documentation-refresh-2026-11-11.md)。

| 历史阶段 | 尚未关闭的事项 |
| --- | --- |
| v0.3 | 真实 Milvus、完整 RAG 工程验收；学习出口已由所有者关闭 |
| v0.4 | 三次真实工具主线仅 2/3 达标；人工语义和学习验收未完成 |
| v0.5 | 固定真实复验第三轮 3/3 通过；完整故障矩阵、长任务／成本对照、语义 Eval 与人工学习仍未完成 |
| v0.6 | 六次真实任务 6/6 通过，目标组比较仅 1/3 达标；默认未切换，独立评审与人工学习未完成 |
| v0.7 | 实施与独立代码审查修复已交付；原设计的独立复审、人工闭卷与延迟复测未完成 |

每项的 revision、测试入口和报告见[验证与验收](../modules/verification.md)。
归档旧方案不关闭遗留项，也不把历史实施授权当作新的功能、提交或外部调用授权。

## v0.8 阅读入口

先读[运行时主线](../modules/runtime.md)，从 Coordinator 跟到图的模型、工具与终态节点。
[ADR-0009](../decisions/ADR-0009-langgraph-only.md) 记录边界与取舍。
[验证记录](../development/v0.8-langgraph-verification.md) 包含学习者的闭卷练习与延迟复测。
