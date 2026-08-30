# v0.3 无 RAG 基线

状态：`Proposed`

日期：2026-08-30

Dataset revision：`v0.3-gold-r1`

记录实现前（无 Prepared Turn Knowledge、无 Citation 合同）系统对知识问题的行为。本基线用于对照
v0.3 工程结果，不是产品缺陷清单。

## 观察方式

在当前 v0.2 Headless JSON v1 上，使用 Fake Model 与（可选）DeepSeek，对 Gold 查询提问。不摄取
任何知识库，Workspace 无 Binding。

## 已冻结结论

1. **无 Citation 合同**：Assistant 完成事件只有 `content` 与 `tool_calls`。Headless JSON v1 没有
   `citations`、`model_supplement` 或 `knowledge.outcome`。
2. **知识问题只能靠模型参数**：Gold 中的公司年假天数、报销阈值、CSV 员工城市等事实不在训练语料
   的项目私有文档里；正确性不可核验，也不保证稳定。
3. **无法区分不可用与无证据**：没有 Binding 时系统不会声明 `NOT_CONFIGURED`；有外部知识故障时
   也不会声明 `UNAVAILABLE`。
4. **删除墓碑不存在**：没有 Source 生命周期，历史回答与原文打开状态未定义。
5. **注入边界仅靠工具权限**：文档级 prompt injection 不会作为 Evidence 注入；v0.2 已阻止模型
   直接调用知识管理（当时尚无知识 CLI）。v0.3 必须继续保证知识命令不进入 Tool Registry。

## 量化占位

在接入 DeepSeek Live Eval 前，Answer/Citation 指标记为“不适用 / 基线为零 Citation”：

| 指标 | 无 RAG 基线 |
| --- | --- |
| Citation Precision | 无 Citation，分母为 0，不定义为通过 |
| Citation Completeness | 0（应引用主张未被引用） |
| 无答案错误知识支持率 | 0（无知识 Citation 可打） |
| Child Recall@10 | 0（未检索） |
| Final PTK Coverage | 0（无 PTK） |

实现后正式数字写入 Eval 报告；本文件保持实现前语义，不事后改写成“原本就会检索”。

## 期望的实现后变化

- 无 Binding：显式 `NOT_CONFIGURED`，回答仅为 Model Supplement。
- 有 Binding 且命中：Knowledge-backed 主张带结构化 Citation。
- 无命中：`INSUFFICIENT` + Supplement，而不是沉默编造项目私有数字。
- 库不可用：`UNAVAILABLE` 或 `PARTIAL`，保留失败库身份。
