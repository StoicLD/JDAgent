# ADR-0004：CLI 应用层与终端 Adapter

状态：`Approved`

日期：2026-08-20

## 背景

v0.1 的 CLI 是验证 Runtime 纵向闭环的薄入口，使用单一 `argparse + input/print` 实现。它已
能创建和恢复 Session、展示流式文本、处理审批和输出安全 Trace，但交互生命周期、命令、终端
编辑、Session 发现、呈现和 Headless 输出都集中在 CLI 附近。继续在该入口中增加功能会让终端
库、Session 存储和 Runtime 编排互相渗透，也难以稳定测试取消、审批和退出。

项目希望参考 Claude Code 等成熟 CLI 的工程经验，但学习目标要求通过独立设计理解 Agent
模块，而不是复制外部项目结构或源码。

## 决策

- 在现有 Turn Coordinator 与 Agent Runtime 之外建立独立 CLI 应用层。
- Interactive Application 负责 REPL 生命周期、当前 Session 引用、交互状态与取消控制；它不
  保存对话历史事实，也不复制 Agent Loop。
- CLI 输入解析为有类型的 `UserAction`；斜杠命令由内部 Command Router 分发，不进入模型。
- Prompt Toolkit 与 Rich 分别作为 Terminal 和 Presentation Adapter；它们的类型不得进入
  Core、领域类型、Runtime Event 或应用用例接口。
- Headless text/json 与交互模式复用同一 Coordinator 和 Runtime，只替换 Presenter 与入口流程。
- Session Catalog 通过面向用例的 seam 提供发现、选择和命名；不得让命令直接扫描 JSONL。
- Session 生命周期事实进入规范事实记录；Catalog 索引仅是可删除、可重建的投影。
- 只在存在真实生产与测试变化点时增加 seam；不为自定义命令、插件或未来 TUI 创建空壳。
- 外部 Claude Code 项目和文档是非规范性设计参考。具体设计必须从本项目批准需求、Python
  技术栈和现有契约推导；不得复制无清晰兼容许可证的源码或把外部目录结构当作默认答案。

## 备选方案

### 继续扩展单一 CLI 文件

短期修改最少，但终端状态、命令、Session 导航和 Runtime 观察会共享可变状态，取消与测试复杂度
会快速扩散，删除 CLI 模块后其复杂度会重新出现在多个调用点。

### 直接建设全屏 TUI

能提供固定消息区、输入区和弹窗，但会提前引入重绘、焦点、窗口尺寸、鼠标和跨终端兼容问题。
v0.2 的核心目标是高频交互闭环，不需要全屏布局。

### 复刻参考项目的五层结构

有成熟实现可对照，但其语言、规模、历史约束和许可证条件与 JDAgent 不同，也会重复现有
Coordinator 与 Agent Loop，损害本项目的学习目标和事实来源纪律。

### 只引入终端库，不增加应用层

可以快速获得多行和颜色，但命令、Session、取消和审批仍会在 Adapter 中编排业务规则，无法形成
稳定测试 seam。

## 后果

- Agent Core 继续保持终端无关；v0.1 Runtime 契约不因 UI 库改变。
- CLI 增加明确的应用状态机、用户动作和 Presentation Model，需要迁移当前 `cli.py` 的职责。
- Prompt Toolkit 和 Rich 成为运行依赖，但被限制在 Adapter 内，未来替换不会影响 Core。
- Scripted Terminal、Capture Presenter 和 Fake Model 可从真实接口驱动完整 REPL 测试。
- Session 发现与恢复需要扩展应用/存储能力，并为 v0.1 本地 Session 提供非破坏性升级路径。
- 模块数量不会按命令数量增长；Command Router 等简单逻辑可保留为深模块内部 seam。
- 参考项目可帮助发现遗漏，但不会成为 JDAgent 的第二事实来源或自动同步上游。

## 复审条件

- 行式 REPL 无法满足已批准的交互需求，需要全屏布局或并行面板。
- Headless、交互、未来 SDK 三种入口无法继续共享同一应用用例。
- Session 存储从单机单 writer 演进为多进程或远程服务，需要重新设计 Catalog 与锁语义。
- 自定义命令或插件获得独立批准，并出现第二种真实命令来源。
- 外部参考的许可证或维护状态发生变化，但任何调整仍须独立评估本项目需求。
