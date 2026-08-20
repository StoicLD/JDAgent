# 当前项目状态

状态：`Implemented`

版本状态：v0.1 `Implemented`；v0.2 `Implemented`（M1–M9 完成，M10 待执行）。

验收状态：v0.1 工程、真实 DeepSeek 与 M10b 人工学习出口全部通过；v0.2 CLI 离线工程门禁、
真实 DeepSeek、PowerShell 主循环和双轴独立审查通过，人工学习出口待执行。

## 当前阶段

v0.1 通用 Agent Runtime 已按批准计划实现并于 2026-08-20 完成最终验收。离线工程门禁、
真实 DeepSeek 流式回答、Tool Call 闭环和 M10b 人工学习出口均已通过。

v0.2 已按批准设计完成 M1–M9：v0.1 薄 CLI 已提升为可安装、可在任意 workspace 日常使用的
持久交互式 CLI。Cursor 计划审查的 F01–F12 已处置；实现后的 Standards/Spec 双轴审查初轮
finding 已全部修复，复核为 0 Blocker、0 High。下一阶段是由学习者完成 M10 人工学习出口。

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
- 2026-08-20 用户批准 [v0.2 CLI 体验需求](../requirements/v0.2-cli-experience.md)、
  [CLI 应用层架构](../architecture/cli-application.md)、
  [ADR-0004](../decisions/ADR-0004-cli-application-layer.md) 与
  [v0.2 实施计划](../plans/v0.2-cli-implementation.md)。
- v0.2 以交互式 CLI 日常可用性为唯一核心结果；Claude Code 与
  `claude-code-best/claude-code` 仅是非规范性设计参考，不是兼容目标、代码来源或第二事实源。
- v0.2 默认真实 Provider 为 DeepSeek；Fake 保留为显式测试能力。CLI 使用增强型行式 REPL，
  不建设全屏 TUI。
- 一个显式 workspace 可以拥有多个 Session；项目配置可共享，Session、输入历史和 UI 状态
  存入按 workspace 分区的本机用户数据目录。
- Session 级允许规则是可审计、可撤销的 Session 事实；`/exit` 后恢复同一 Session 时继续有效，
  但不得覆盖用户或项目的 `DENY`，也不得泄漏到其他 Session。

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
- 分层 Configuration Resolver、DeepSeek 默认 Provider、开发 Key 回退和稳定 Headless
  text/json/退出码合同。
- Prompt Toolkit 行式 REPL、Rich 流式 Markdown 与持续状态 footer、九条内建斜杠命令、历史、
  多行输入和 workspace 级 Session Catalog。
- Session 生命周期事实、名称/短 ID 解析、legacy 非破坏导入、物理尾行修复、逻辑未完成 Turn
  分类与副作用不确定时的恢复快照。
- 可取消 Turn 与可恢复主循环；运行中 `Ctrl+C` 返回 idle，空闲 `Ctrl+C` 返回 130，`/exit`/EOF
  正常保留 Session。
- 可审计/可撤销的 Session 文件或目录允许规则；恢复同一 Session 后仍有效，新 Session 不继承，
  且不能放宽用户或项目 `DENY` 上限。

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
- 2026-08-20：Cursor 对 v0.2 计划 revision `6f34007a4e7bbf66f39c12237ef30c4f51815e5c`
  完成独立审查；F01–F12 的正式结论和前置门禁见
  [v0.2 CLI 独立审查处置](../reviews/REV-20260820-001-v0.2-cli-plan-disposition.md)。
- 2026-08-20：v0.2 最终离线门禁为 `113 passed, 3 skipped`，Ruff format/check、Pyright strict、
  `git diff --check` 通过；三项显式 opt-in DeepSeek live 测试全部通过。
- 真实 PowerShell PTY 验证了九条命令的主路径、Session 恢复、运行中取消后继续使用和 `/exit`；
  最终 `0.2.0` wheel 在源码树外隔离安装后，console script 与 `python -m jdagent` 的 JSON
  Headless 入口均通过。
- v0.2 双轴实现审查的 Standards 与 Spec 复核均为 0 Blocker、0 High；完整发现、修复与残余
  人工环境风险见 [v0.2 实现审查](../reviews/v0.2-implementation-review.md)。

## v0.1 最终验收

- 用户已于 2026-08-20 完成 [M10b 学习检查表](../learning/v0.1-learning-checklist.md)：闭卷
  模块关系、Tool Call 数据流和 Trace 故障练习均确认完成。
- v0.1 没有剩余验收项。

## 下一步

1. 完成 [v0.2 CLI 学习检查表](../learning/v0.2-learning-checklist.md) 的闭卷架构、五条数据流和
   恢复分类练习。
2. 在人工环境矩阵中继续观察中文 IME、legacy console 与真实窗口强关；强关不承诺执行 finally，
   恢复仍以已批准的 Session 合同为准。
3. M10 完成前不启动 MCP、RAG、求职领域能力、全屏 TUI、自定义命令或通用 Session Branch。

## 恢复入口

- [项目文档索引](index.md)
- [仓库 Agent 地图](../../AGENTS.md)
