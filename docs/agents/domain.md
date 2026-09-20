# 领域文档约定

状态：`Approved`

批准日期：2026-09-20

采用 single-context 布局，适配本仓库已有的权威文档结构。

## 阅读顺序

1. 完成 `AGENTS.md` 规定的固定启动顺序。
2. 通过 `docs/context/index.md` 定位当前任务涉及的领域、架构和合同。
3. 涉及知识检索时，阅读 `docs/context/v0.3-rag-glossary.md`。
4. 阅读 `docs/decisions/` 中与当前任务有关的 ADR。

## 路径映射

- 技能中的 `CONTEXT.md`：对应索引指向的领域文档与词汇表。
- 技能中的 `docs/adr/`：对应本仓库 `docs/decisions/`。
- 新领域术语进入相应领域文档；新 ADR 沿用现有目录和编号约定。
- 本次不另建 `CONTEXT.md`、`CONTEXT-MAP.md` 或 `docs/adr/`。

## 使用规则

- 使用对应领域文档中的术语，并遵守其适用范围。
- 发现术语缺口时，在领域建模任务中处理。
- 方案与已有 ADR 冲突时，明确引用冲突及复审理由；
  提案不自动覆盖已批准决策。
- 必需入口缺失或权威文档冲突时，遵循项目协作规则处理。
