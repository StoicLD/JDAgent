# ADR-0003：通用 Runtime 与求职领域分离

状态：`Approved`

日期：2026-08-09

## 背景

项目第一阶段用于掌握通用 Agent 原理；第二阶段要解决职位、面经和技术经验采集分析问题。如果领域逻辑直接进入 Core，将无法判断抽象是否通用，也难以独立测试和复用。

## 决策

- 第一阶段建立领域无关的 Agent Runtime。
- 第二阶段以 Job Domain Pack 接入求职工具、知识、检索、工作流和 Eval。
- Agent Core 不包含网站名称、职位和简历领域对象、求职 Prompt 或特定抓取规则。
- 领域包通过 Tool Registry、Context Source、Memory/Retrieval 和 Eval 接口扩展 Runtime。

## 备选方案

- 从求职产品直接提炼 Core：能更早展示领域价值，但容易在尚未理解机制时把特例固化为架构。
- 通用 Runtime 与求职 Agent 分成两个项目：边界清楚，但会造成重复集成和版本协调。

## 后果

- v0.1 只验证通用机制，不以求职功能数量衡量完成度。
- 第二阶段成为对 Runtime 扩展性的真实验证。
- 领域特例必须留在 Domain Pack，即使把它写进 Core 更快。

## 复审条件

当真实求职用例证明某项能力在多个领域都成立时，可通过独立 ADR 将其提升为 Runtime 能力。

