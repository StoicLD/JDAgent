# 当前项目状态

状态：`Implemented`（状态记录）

更新日期：2026-10-22。实现说明的代码核对基准：`12196d6895f97fdc140c6f46aee34ce90aabe4c8`。
v0.5 设计接续时仓库 HEAD：`b54a20132cc9eab7b33f2be7c4ee2b2ad1238621`；本轮未修改产品代码。
R1 设计修订基于 `f5d0ba7c82f4257e865e3440a451d4178df3c2bb`，只修改文档。

## 当前结论

通用 Agent Runtime、持久 CLI、RAG P1 和 v0.4 八工具已有代码。
包版本仍为 `0.3.0`；功能阶段 v0.4、包版本、Session 格式是不同概念。
当前实现按模块阅读 [文档 Map](../map.md)，验证细节见[验证与验收](../modules/verification.md)。

| 阶段 | 工程状态 | 学习状态 |
| --- | --- | --- |
| v0.1 Runtime | 已完成验收 | 2026-08-20 完成 |
| v0.2 CLI | 已完成验收 | 2026-08-24 所有者按学习范围裁定关闭 |
| v0.3 RAG | P1 代码已实现；工程复核未通过，Milvus Live 未完成 | 2026-09-16 所有者裁定关闭 |
| v0.4 工具执行 | 八工具和离线链已实现；真实 DeepSeek 三次主线仅 2/3 通过 | 未完成，人工语义验收待学习者 |
| v0.5 Context 与 Memory | 产品范围已确认；Cursor 初审 fail，R1 文档修订及逐项处置已形成；工程细化仍为 Proposed，待复审，尚未实现 | 初始模型已记录，学习清单待执行 |

最近保留的完整离线结果为 2026-09-23 的 `236 passed, 1 failed, 8 skipped`；
唯一失败是 v0.3 词项 Recall `0.8125 < 0.85`。这不是 2026-10-20 重新运行完整套件的结果。

## 当前待办

1. v0.4：处理显式任务步骤遗漏，再按新的固定 revision 验证，保留原三次尝试；
   完成人工语义验收与[学习出口](../learning/v0.4-tools-learning-checklist.md)。
2. v0.3：继续解决恢复、物理删除、历史引用核验、真实配置和 Eval 缺口；
   详情见[知识模块](../modules/knowledge.md)及其关联评审。M0 详细需求／合同／Eval 仍为 `Proposed`。
3. v0.5 合并上下文、压缩、跨会话检索和长期记忆，见[已确认规格](../requirements/v0.5-context-memory.md)。
   下一步按 [Cursor 逐项处置](../reviews/REV-20261021-001-v0.5-context-memory-design-review.md)复审
   [R1 工程细化与阶段计划](../plans/v0.5-context-memory-implementation.md)，按现有门禁取得实施授权。
   后续版本号待定，MCP 仍为候选；[学习路线](../plans/post-v0.2-learning-roadmap.md)不替代实施授权。

## 授权与证据边界

- v0.4 已于 2026-09-23 获得实施授权，详见[实现记录](../plans/v0.4-implementation.md)。
  历史提案的 `Proposed` 不表示实现未获授权。
- Q23 按实际依赖判断 v0.3 遗留项对 v0.4 的阻塞范围；相关权限、会话、工具与基础回归须处理，
  无关 RAG 质量缺口独立跟踪。该裁定不等于 v0.3 工程通过。
- 2026-10-21 用户确认 v0.5 访谈收敛方案，授权形成设计文档，并在文档完成后清理已确认的旧会话。
  新增工程细节仍标 Proposed；没有产品实现、Git 提交或推送授权。清理结果见
  [一次性过渡记录](../development/v0.5-session-reset.md)。既有工程与学习验收结论保持原状态。
  2026-10-20 的文档整理没有批准新功能或未决提案。
  历次批准、验证时间线和旧状态叙述保存在[整理前状态记录](../archive/context/current-before-2026-10-20.md)；
  长期架构决策从 Map 中的有效 ADR 进入。
- 2026-10-22 用户授权按维护方评估修订 v0.5 设计并补 Cursor 逐项处置，明确暂不开始代码实现。
  原始审查结论保留；维护方文档检查不等于 Cursor 复审通过，也不关闭已有工程或学习门禁。

## 恢复入口

[文档 Map](../map.md) · [协作规则](../development/agent-collaboration.md) · [Agent 入口](../../AGENTS.md)
