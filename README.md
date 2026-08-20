# JDAgentProject

状态：`Implemented`

版本状态：v0.1 `Implemented`；v0.2 `Implemented`（M1–M9 完成，M10 学习出口待执行）。

验收状态：v0.1 最终验收完成；v0.2 CLI 工程门禁、Live 验收与双轴独立审查通过。

JDAgent 是一个通过实践学习 Agent 核心机制的项目，当前工作名尚不代表未来公开发布名称。

项目分为两个阶段：

1. 构建领域无关、模型无关、可观察且可测试的通用 Agent Runtime，系统掌握 LLM、Function Calling、工具、权限、会话、上下文、记忆、RAG 和 MCP 等机制。
2. 在通用 Runtime 上增加求职领域包，接入职位、面经和技术经验来源，并针对采集、检索、分析和质量评测做领域优化。

v0.1 通用 Agent Runtime 已完成最终验收，包含模型无关 `ModelPort`、Agent Loop、
ContextBuilder、三种工具、权限审批、append-only JSONL Session、恢复、Trace、Fake Model
与 DeepSeek Adapter。离线门禁、真实 DeepSeek 流式文本与 Tool Call 闭环、人工学习出口均已
通过；MCP、RAG、长期记忆和求职领域包不属于 v0.1。

v0.2 已实现可安装的持久交互式 CLI，包括增强型行式 REPL、九条内建斜杠命令、多 Session
发现与恢复、可取消 Turn、可读审批、异常关闭恢复，以及稳定 text/json 输出。设计范围见
[v0.2 CLI 体验需求](docs/requirements/v0.2-cli-experience.md) 和
[CLI 应用层架构](docs/architecture/cli-application.md)。

## 当前 v0.2 快速开始

需要 Python 3.11 和 [uv](https://docs.astral.sh/uv/)：

```powershell
uv sync
uv run jdagent
```

不带 Prompt 时进入交互模式；使用 `/help` 查看命令，`/sessions` 发现当前 workspace 的会话，
`/resume <名称或ID>` 恢复，`/exit` 正常退出并保留当前 Session。一个 workspace 可拥有多个
Session；Session、输入历史和 Catalog 位于按 workspace identity 分区的本机用户数据目录，
不污染工作区。

默认 Provider 是 DeepSeek。离线开发和确定性测试应显式使用 Fake：

```powershell
uv run jdagent --provider fake
uv run jdagent --provider fake --output json "离线测试"
```

Headless text/json 输出和显式 Session 恢复示例：

```powershell
uv run jdagent "解释 ModelPort"
uv run jdagent --output json --show-trace "解释 ModelPort"
uv run jdagent --session-id <SESSION_ID> "继续"
```

模型、base URL、模型超时、工具超时和可选硬上下文限制均可通过 CLI 参数配置；运行
`uv run jdagent --help` 查看完整选项。

DeepSeek Key 来源按以下顺序解析：

1. `DEEPSEEK_API_KEY` 环境变量。
2. 用户配置中的 `api_key_file`。
3. 开发期默认文件 `../tmp/keys/deepseek-api-key.txt`。

默认文件位于产品仓库之外，不得加入 Git；环境变量始终优先。生产或共享环境应使用批准的
秘密管理方案，而不是依赖开发机文件。

```powershell
$env:DEEPSEEK_API_KEY = "..."
uv run jdagent "用 calculator 计算 17 * 23"
```

写文件工具受用户/项目权限上限约束。交互审批可选择仅本次、当前 Session 的具体文件或目录；
Session 规则在 `/exit` 后恢复同一 Session 时继续有效，可用 `/permissions` 查看和撤销，但新建
Session 不继承。Headless 模式不会阻塞等待审批。读写路径始终限制在显式 workspace 内。

## 质量门禁

```powershell
uv run ruff format --check .
uv run ruff check .
uv run pyright
uv run pytest -q
```

真实 Provider 测试默认跳过。显式设置 `JDAGENT_RUN_DEEPSEEK_INTEGRATION=1` 后运行
`tests/integration/test_deepseek_live.py`；测试同样遵循上述 Key 优先级。

## v0.1 学习出口

[v0.1 学习检查表](docs/learning/v0.1-learning-checklist.md) 已于 2026-08-20 完成。检查表包含
闭卷架构图、Tool Call 数据流讲解和一个无需真实 API 的 Fake Model 超时 Trace 故障练习。

下一步使用 [v0.2 CLI 学习检查表](docs/learning/v0.2-learning-checklist.md) 完成 M10，验证 CLI
应用层、Session Catalog、终端 Adapter、输出合同和异常恢复理解。

恢复工作请依次阅读：

1. [Agent 地图](AGENTS.md)
2. [当前项目状态](docs/context/current.md)
3. [项目文档索引](docs/context/index.md)
