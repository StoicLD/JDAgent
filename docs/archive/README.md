# 历史文档归档

状态：`Implemented`（归档整理）

最近归档：2026-11-11。本目录保存历史需求／计划、被后续实现替代的设计叙述和状态时间线。
当前代码是产品行为的唯一事实来源，当前实现从[文档 Map](../map.md)和[架构总览](../architecture/overview.md)阅读。

## 2026-11-11 归档

[本次索引](2026-11-11/README.md)逐项列出 32 份材料、迁移原因和替代入口，
[manifest](2026-11-11/manifest.json)保留原路径和迁移前指纹。
覆盖 v0.3–v0.7 设计／需求／计划、旧 ModelPort 设计、阶段验收与实验文档、术语讨论和旧状态／Map。
归档文件保留原批准状态；其中未实现目标、失败测试和未完成人工验收不因搬迁而关闭。

## 如何解释归档

归档改变阅读入口和文件位置，不重写原始审批、发现或验收结果。
原文中的 `Approved`、`Proposed` 等状态属于当时材料；本次不把它们统一改成
`Superseded`，因为部分已批准约束仍适用，且未决条目不因搬迁获得批准。
仍适用的设计约束应结合后续授权、合同细化和 ADR 阅读。

模块说明描述现有行为；ADR 记录历史架构取舍；当前状态记录限制与未决验证。
需要追溯需求或设计理由时查这里，不能把旧版本专属行为直接当成当前实现。
所有相对链接随迁移调整，评审报告和原始验收证据仍留在 `docs/reviews/`。

## v0.1

| 归档材料 | 归档原因与当前入口 |
| --- | --- |
| [Agent Runtime 需求](v0.1/agent-runtime-requirements.md) | 首版验收已完成；通用边界继续有效，当前功能范围见[Runtime](../modules/runtime.md) |
| [实施计划](v0.1/implementation.md) | 里程碑已完成，保留历史验收约定 |
| [Runtime 架构](v0.1/agent-runtime.md) | 三工具、无 Shell、Future RAG 等是早期范围；当前见[Runtime](../modules/runtime.md)、[工具](../modules/tools.md)、[知识](../modules/knowledge.md) |
| [Runtime 核心契约](v0.1/runtime-contracts.md) | 包含事件 v1、v0.2 待实现叙述；当前见[会话](../modules/sessions.md)与[工具](../modules/tools.md)，原停止语义等仍可追溯 |

## v0.2

| 归档材料 | 归档原因与当前入口 |
| --- | --- |
| [CLI 需求](v0.2/cli-requirements.md)、[实施计划](v0.2/implementation.md) | 阶段验收已完成；当前见[CLI](../modules/cli.md) |
| [CLI 架构](v0.2/cli-application.md) | 九命令、legacy 导入等属于旧阶段；当前已有知识／日志命令和新会话分区 |
| [Session 生命周期](v0.2/session-lifecycle.md) | 保留身份／事件设计来源，当前 schema 与配对语义见[会话](../modules/sessions.md) |
| [异常恢复设计](v0.2/session-recovery.md) | 当前进一步覆盖 EXECUTE、未知结果与日志读取 |
| [Permission Rule 合同](v0.2/permission-rules.md) | 当前扩展命令规则并调整审批展示，见[工具](../modules/tools.md) |
| [Windows 终端附录](v0.2/windows-terminal.md) | 保留当时行为与人工矩阵；迁移不表示矩阵已全部通过 |

## v0.4

| 归档材料 | 归档原因与当前入口 |
| --- | --- |
| [工具执行层设计提案](v0.4/tool-execution-proposal.md) | 历史 `Proposed` 提案；后续已授权实现并细化默认值、旧 Session 处理和 Windows 提交机制 |
| [23 题设计讨论](v0.4/design-discussion.md) | 保留取舍与 Q23 裁定，日常实现入口改为[工具模块](../modules/tools.md) |

v0.4 的[实施记录](2026-11-11/plans/v0.4-implementation.md)保留历史授权和合同细化过程。
工程与学习仍有开放项，归档提案不代表阶段已验收。

## 状态时间线

[整理前的 current.md](context/current-before-2026-10-20.md)保留历次批准、提交、
验证与学习裁定的完整记录。[2026-11-11 整理前快照](2026-11-11/context/current.md)保留后续时间线。
当前工作入口是[精简状态页](../context/current.md)。

## 继续保留在当前目录的材料

ADR、开发规则、学习检查表、评审与证据保留原分类。当前 ModelPort 合同已按代码重写，原设计归档。
评审正文只调整受迁移影响的导航链接；原结论、revision 和指纹保持不变。
学习清单用于各阶段练习和历史记录，人工未完成项仍由学习者执行。
