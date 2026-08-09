# ADR-0001：Python 作为唯一 Agent Core 语言

状态：`Approved`

日期：2026-08-09

## 背景

项目需要同时服务于 Agent 核心机制学习，以及后续求职领域中的网页采集、文本处理、RAG 和数据分析。Python 与 TypeScript 都具备 Agent 生态，但双核心会增加实现、测试和认知负担。

## 决策

- Agent Core 使用 Python 3.11+。
- 不维护第二套 TypeScript Agent Core。
- 后续 Web UI 可以使用 TypeScript，但只能通过稳定接口调用 Python Runtime。
- 采用严格类型、异步 I/O、端口与适配器以及自动测试约束 Python 工程质量。

## 备选方案

- TypeScript Core：更接近部分 Coding Agent CLI 和 Web 全栈生态，但不如 Python 适合后续 RAG、采集和数据实验的统一路线。
- Python 与 TypeScript 双核心：可以比较语言实现，但会复制行为和测试，不利于本项目的学习目标。

## 后果

- Agent、RAG、爬虫和数据处理可共享同一语言生态。
- 需要主动治理 Python 类型、异步资源生命周期和模块边界。
- Python 包名和发布名称仍需在首次工程初始化前确认。

## 复审条件

只有在 Python 无法满足经过验证的运行时、分发或 UI 集成需求时复审；偏好或框架流行度变化不足以触发双核心。

