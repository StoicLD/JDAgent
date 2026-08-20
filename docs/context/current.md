# 当前项目状态

状态：`Implemented`

验收状态：工程与真实 DeepSeek 验收通过；M10b 人工学习出口待完成。

## 当前阶段

v0.1 通用 Agent Runtime 已按批准计划实现。离线工程门禁、真实 DeepSeek 流式回答和
Tool Call 闭环均已通过。当前只剩 M10b 人工学习出口必须由用户本人完成，因此尚不把整个
v0.1 标记为最终验收完成。

## 已批准事实

- `JDAgentProject` 是产品与工程事实的唯一权威仓库。
- `JDAgentAgentContext` 是同级私有 Agent Context 仓库，不是产品构建、测试或运行依赖。
- 第一阶段建设领域无关的通用 Agent Runtime；第二阶段通过求职领域包进行特化。
- Agent Core 使用 Python 3.11+，不维护第二套 TypeScript Core。
- Agent Core 依赖模型无关的 `ModelPort`；首个真实 Provider Adapter 对接 DeepSeek。
- 项目权威文档使用中文；未来代码标识符、公共 API、Docstring 和错误类型使用英文。
- [v0.1 Agent Runtime 需求](../requirements/v0.1-agent-runtime.md) 已于 2026-08-09 获得用户批准。
- Agent Runtime 架构、核心契约、ModelPort、Python 工程规范和实施计划已于 2026-08-12
  获得用户批准。
- Python import package 与 distribution 均使用 `jdagent`；最低版本为 Python 3.11，当前
  `.python-version` 固定 3.11.15。
- DeepSeek Adapter 默认模型为 `deepseek-v4-flash`，默认 base URL 为
  `https://api.deepseek.com`；v0.1 显式关闭 thinking，避免丢失未纳入事件模型的 reasoning
  内容。
- 2026-08-19 用户批准开发期 DeepSeek Key 回退：显式 `DEEPSEEK_API_KEY`/配置值优先，
  缺省读取工作区 `tmp/keys/deepseek-api-key.txt`。该文件位于产品仓库外，不得进入 Git。

## 已实现范围

- 模型无关 `ModelPort`、脚本化 Fake Model 与 DeepSeek SSE Adapter。
- 基于规范事件投影的薄 ContextBuilder 与小型 Agent Loop。
- Tool Registry、JSON Schema 校验、集中式 workspace 路径解析、`calculator`、
  `read_text_file`、`write_text_file`。
- `ALLOW | ASK | DENY` 权限策略、CLI 审批与副作用前检查。
- InMemory 与 append-only JSONL Session、显式 resume、损坏数据 fail closed。
- 同源 Runtime Event 的 Trace 投影、Usage、超时、取消与调用预算。
- CLI Composition Root；Core 不依赖 Provider SDK、文件系统、环境变量或具体 Session Adapter。
- CLI 支持安全结构化 `--show-trace`，以及模型/工具超时和硬上下文限制配置；Trace 不输出
  消息正文、工具参数或文件内容。

## 验证与评审证据

- 2026-08-12：Ruff format/check、Pyright strict 均通过。
- 2026-08-19：默认测试 `61 passed, 2 skipped`；两项跳过均为显式 opt-in 的 DeepSeek
  live integration tests。启用 live 后完整测试 `63 passed`。
- 真实 DeepSeek 流式文本与 Tool Call 闭环均通过；执行时未设置 `DEEPSEEK_API_KEY`，证明
  默认开发 Key 文件回退有效。
- 双轴审查初轮发现取消传播、Composition Root、Trace 细节、resume、finish reason、空
  JSONL 与秘密忽略规则等阻塞问题；全部完成修复并增加回归测试。
- 2026-08-19 补充审查：Standards 为 `pass_with_findings`、0 个硬违规，仅保留开发路径依赖
  源码布局的非阻塞判断项；Spec 为 `pass`、0 findings。
- 完整记录见 [v0.1 实现审查](../reviews/v0.1-implementation-review.md)。

## 剩余验收项

- 用户完成 [M10b 学习检查表](../learning/v0.1-learning-checklist.md)：闭卷画出模块关系、
  解释 Tool Call 数据流，并使用 Trace 定位一次注入失败。

## 下一步

1. 与用户进行 M10b 学习复盘；未理解的机制进入 v0.2 学习输入。
2. M10b 完成后把 v0.1 状态升级为最终验收完成，并讨论 v0.2，不提前引入 MCP、RAG
   或求职领域能力。

## 恢复入口

- [项目文档索引](index.md)
- [仓库 Agent 地图](../../AGENTS.md)
