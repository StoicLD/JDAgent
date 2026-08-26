# Agent 协作与评审规则

状态：`Approved`

批准日期：2026-08-26

## 目的与适用范围

本文件定义 JDAgent 的事实归属、角色边界、写入纪律、独立评审接口和并行工作规则。它适用于
Codex、Cursor、Claude 以及以后接入的其他 Agent，但不规定工具私有的提示词、会话状态或内部
工作方式。

## 唯一事实来源

- 当前产品 Git 仓库统一保存需求、验收标准、架构、ADR、计划、代码、测试、当前状态和正式
  评审处置；每项事实只有一个权威位置。
- 根部原生入口和文档索引只负责导航，不复制详细项目事实。
- 聊天摘要、Agent 私有记忆、草稿、缓存和原始临时输出不是项目事实，不得成为构建、测试、
  运行、恢复或正式交接依赖。
- 私有记录与仓库事实冲突时，私有记录视为过时或未验证；两份正式文档冲突时停止写入，依据
  最近批准的决策、验证证据和用户裁定修复冲突。

## 角色与批准边界

### 项目所有者

- 批准需求、范围、验收标准、架构、ADR、计划、技术选择和重大风险处置。
- 决定是否扩大 Agent 权限，以及是否执行 commit、push、merge、历史改写或破坏性操作。
- 按[模块交付与学习工作流](module-delivery-workflow.md)亲自承担不能外包的学习判断与出口。

### 主要设计与实现 Agent

- 澄清需求、提出可比较方案、实现已批准范围、编写测试、维护权威文档并处置评审发现。
- 不把未批准提案写成既定事实，不擅自扩大范围，也不以代码生成代替验收证据。
- 讨论和诊断保持只读；实现时先检查现有接口、测试、失败模式和工作树重叠。

### 独立分析与评审 Agent

- Cursor、Claude 或其他被指定评审方默认只读，依据固定 revision 或显式工作树快照独立判断。
- 形成自己的结论前，不读取另一评审方尚未定稿的原始报告；结论趋同不降低取证强度，结论分歧
  也不通过修改对方状态来对齐。
- 不直接批准需求、架构或决策，不修改产品实现或当前状态，除非用户针对当前任务明确授权。

## 写入与证据纪律

- 同一工作树同一时刻只有一个写入者。开始写入前检查 branch、HEAD、remote、未提交变更和
  路径所有权；不覆盖或回退不属于当前任务的用户改动。
- 低风险整理使用有针对性的验证；跨模块、数据迁移、安全敏感或外部系统变更提高测试、失败
  分析和独立评审强度。
- 完成声明必须引用真实文件、Git diff、测试、静态检查、Runtime Event、Trace、日志或可复现
  实验；AI 解释只作为待验证的专家输入。
- 代码、测试和文档出现冲突时，把它作为需修复的不一致，不预设其中一方永远优先。
- 未经用户明确授权，不 commit、pull、push、merge、切换分支、改写历史或配置 remote。

## 正式评审接口

### 评审请求

正式评审至少包含：

```yaml
review_id: REV-YYYYMMDD-NNN
reviewer: cursor | claude | <other>
reviewed_revision: <full commit SHA or explicit working-tree snapshot>
scope:
  - <files, modules, or behaviors>
requirements:
  - <approved requirements and acceptance criteria>
verification:
  - <commands the reviewer may run>
out_of_scope:
  - <explicit exclusions>
```

同一自然日的 `NNN` 在所有评审方之间唯一。取号时只检查已有评审的编号和文件名，不读取其他
评审方尚未定稿的正文。

### 评审发现

每个发现至少包含：

```yaml
finding_id: REV-YYYYMMDD-NNN-F01
severity: blocker | high | medium | low
location: <file and line, test, log, or document>
claim: <what is wrong>
evidence: <why the claim is supported>
impact: <what can happen>
recommendation: <a possible correction>
```

报告还要列出实际运行的命令、未运行检查及原因、剩余风险，以及 `pass`、
`pass_with_findings` 或 `fail` 结论。原始报告可以放在宿主临时区或外层本地 `tmp/`，但只有
经处置后进入 `docs/reviews/` 的内容才是保留的项目事实。

### 发现处置

主要维护 Agent 对有实际影响的发现逐项记录：

```yaml
finding_id: REV-YYYYMMDD-NNN-F01
disposition: accepted | rejected | deferred
rationale: <reason and evidence>
resolution_revision: <commit SHA, when applicable>
```

- `accepted`：修复并验证。
- `rejected`：依据需求、代码、测试或适用范围说明理由。
- `deferred`：说明风险、延后原因和重新触发条件。

重大争议交由项目所有者裁定；评审报告和维护 Agent 的回应都不自动覆盖用户决定。

## 并行工作与临时内容

- 并行实现使用按任务创建的独立 branch/worktree，固定基准 revision，分配非重叠路径所有权，
  定义验证命令，并指定唯一合并负责人；worktree 不是按 Agent 永久分仓。
- 工具私有的短期状态优先使用宿主原生 session/memory。不得在产品仓库建立按 Agent 划分的
  Context 目录，也不得把私有状态当作 Agent 间的隐式通信通道。
- 临时日志、原始评审、导出和可重建实验可进入本地临时区；需要长期保留的结论先验证，再晋升
  到对应权威文档，不长期复制两份。
- 密钥、Token、账号密码、支付信息和不必要的个人敏感数据不得进入仓库、普通评审报告或聊天
  摘要；使用已批准的配置与秘密管理路径。

## 失败行为

遇到以下情况时停止写入，保留现场并向用户报告：

- 原生入口、共同索引或必需权威文档缺失或链接失效；
- 正式事实互相冲突，且没有足够证据确定应保留的版本；
- Git 顶层、branch、HEAD、remote、未提交变更或评审 revision 无法确认；
- 当前任务需要扩大权限、修改 Git 边界、处理他人改动或执行破坏性操作；
- 验证无法运行，或证据不足以支持完成声明。
