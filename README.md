# JDAgentProject

状态：`Implemented`

验收状态：离线与真实 DeepSeek 验收通过；M10b 人工学习出口待完成。

JDAgent 是一个通过实践学习 Agent 核心机制的项目，当前工作名尚不代表未来公开发布名称。

项目分为两个阶段：

1. 构建领域无关、模型无关、可观察且可测试的通用 Agent Runtime，系统掌握 LLM、Function Calling、工具、权限、会话、上下文、记忆、RAG 和 MCP 等机制。
2. 在通用 Runtime 上增加求职领域包，接入职位、面经和技术经验来源，并针对采集、检索、分析和质量评测做领域优化。

v0.1 通用 Agent Runtime 已完成离线实现，包含模型无关 `ModelPort`、Agent Loop、
ContextBuilder、三种工具、权限审批、append-only JSONL Session、恢复、Trace、Fake Model
与 DeepSeek Adapter。真实 DeepSeek 流式文本与 Tool Call 闭环已经通过；MCP、RAG、长期记忆
和求职领域包不属于 v0.1。

## 快速开始

需要 Python 3.11 和 [uv](https://docs.astral.sh/uv/)：

```powershell
uv sync
uv run jdagent "你好"
```

默认使用离线 Fake Model，不访问网络。Session 默认保存在工作区的 `.jdagent/sessions/`。
继续已有会话时使用首次运行输出的 Session ID：

```powershell
uv run jdagent --session-id <SESSION_ID> "继续"
```

检查一轮不包含消息正文、工具参数或文件内容的结构化 Trace：

```powershell
uv run jdagent --show-trace "解释 ModelPort"
```

模型、base URL、模型超时、工具超时和可选硬上下文限制均可通过 CLI 参数配置；运行
`uv run jdagent --help` 查看完整选项。

真实 DeepSeek 调用必须显式选择 Provider。Key 来源按以下顺序解析：

1. `DEEPSEEK_API_KEY` 环境变量。
2. 开发期默认文件 `../tmp/keys/deepseek-api-key.txt`。

默认文件位于产品仓库之外，不得加入 Git；环境变量始终优先。生产或共享环境应使用批准的
秘密管理方案，而不是依赖开发机文件。

```powershell
$env:DEEPSEEK_API_KEY = "..."
uv run jdagent --provider deepseek "用 calculator 计算 17 * 23"
```

写文件工具始终要求交互式批准；读写路径均被限制在 `--workspace` 内。

## 质量门禁

```powershell
uv run ruff format --check .
uv run ruff check .
uv run pyright
uv run pytest -q
```

真实 Provider 测试默认跳过。显式设置 `JDAGENT_RUN_DEEPSEEK_INTEGRATION=1` 后运行
`tests/integration/test_deepseek_live.py`；测试同样遵循上述 Key 优先级。

## M10b 学习出口

在开始 v0.2 前完成 [v0.1 学习检查表](docs/learning/v0.1-learning-checklist.md)。检查表包含
闭卷架构图、Tool Call 数据流讲解和一个无需真实 API 的 Fake Model 超时 Trace 故障练习。

恢复工作请依次阅读：

1. [Agent 地图](AGENTS.md)
2. [当前项目状态](docs/context/current.md)
3. [项目文档索引](docs/context/index.md)
