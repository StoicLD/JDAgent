# JDAgent

JDAgent 是用于学习和实践 Agent 工程的 Python 命令行程序。它可以调用 DeepSeek，读取与修改
项目文件、执行命令、保存和恢复会话、压缩上下文、检索历史与记忆，并连接 MCP 服务。
主要运行环境为 Windows，文件写入和进程管理使用 Windows 实现。

当前代码是产品行为的唯一事实来源。v0.8 的执行路径于 **2026-11-19** 核对，改造前基准为 `9d7cbdc`；包版本仍为
**0.5.0**，包含后来加入的 LangGraph、LangChain 和 MCP 能力。v0.1–v0.7 是历史开发阶段，
不能用来推断当前包版本或验收结果。

## 五分钟开始

需要 Git、uv 和 Python 3.11+；[.python-version](.python-version) 指定开发解释器版本。
以下命令在 PowerShell 中执行。已有本地仓库时，进入 `JDAgentProject` 后从 `uv sync` 开始。

```powershell
git clone https://github.com/StoicLD/JDAgent.git JDAgentProject
Set-Location JDAgentProject
uv sync --locked
uv run jdagent --version
uv run jdagent --provider fake --output json "离线测试"
```

预期版本输出 `jdagent 0.5.0`，离线回答为 `Offline fake model response.`。
Fake 只验证运行链路，不理解问题或按自然语言操作文件。新克隆只包含已推送内容。

使用真实模型，在同一终端设置 Key 后启动：

```powershell
$env:DEEPSEEK_API_KEY = [System.Net.NetworkCredential]::new('', (Read-Host 'DeepSeek API Key' -AsSecureString)).Password
uv run jdagent --provider deepseek --tool-scope analyze
```

程序默认模型名为 `deepseek-v4-flash`，可以用 `--model` 覆盖。输入问题开始对话，`/help` 查看命令，
`/exit` 退出。安装、密钥文件、恢复会话和排错见[完整使用教程](docs/guides/usage.md)。

## 常用操作

在产品仓库目录执行，将工作区替换为已经存在的项目路径：

| 目的 | PowerShell 命令 |
| --- | --- |
| 阅读与搜索项目 | `uv run jdagent --workspace 'E:\Projects\Example' --tool-scope analyze` |
| 创建与修改文件 | `uv run jdagent --workspace 'E:\Projects\Example' --tool-scope edit` |
| 编辑并运行程序 | `uv run jdagent --workspace 'E:\Projects\Example' --tool-scope execute --command-permission ask` |
| 单次 JSON 回答 | `uv run jdagent --tool-scope analyze --output json --show-trace "解释项目的启动流程"` |
| 使用 LangChain 接入 DeepSeek | `uv run jdagent --model-adapter langchain --tool-scope analyze` |
| 续做已有记忆整理作业 | `uv run jdagent maintain --pending --workspace 'E:\Projects\Example'` |

每轮统一由 LangGraph 调度。DeepSeek 默认使用原生模型适配器，也可选 LangChain。
升级到 v0.8 后，删除旧脚本的 `--runtime-engine` 或用户 TOML 中的 `runtime_engine`。两个旧值都会启动 LangGraph，并在 stderr 提示一次。
默认工具范围为 `execute`，写入与命令需要审批；单次运行不等待人工审批。
程序在本机执行命令，不提供不可信代码沙箱。

下面输入到 **JDAgent 交互界面**：

```text
/rename 项目梳理
/sessions
/context-window
/mcp status
/trace
/exit
```

`/resume <名称或ID>` 恢复会话，`/new` 开始新会话，`/compact` 在空闲时压缩上下文。
长期记忆使用“记住：项目使用 pytest”等直接输入，详见[历史与记忆](docs/guides/usage.md#历史检索与长期记忆)。

## 能力与边界

| 范围 | 当前实现 |
| --- | --- |
| 执行与模型 | LangGraph 调度并顺序执行工具。DeepSeek 可选原生或 LangChain 适配器 |
| 本地工具 | 计算、文件查找／读取／搜索／创建／精确编辑、命令执行及输出分页读取 |
| 会话与上下文 | JSONL 事件、独占锁、异常恢复、请求归档、预算与压缩；压缩保留原始历史 |
| 历史与记忆 | 同工作区历史检索、项目／用户记忆、版本与来源、后台整理；未配置 Embedding 时关键词检索 |
| MCP | stdio 与 HTTP、工具／资源／Prompt、BM25 检索、有界缓存、原文读回和顺序等待 Task |
| 知识库 | TXT／Markdown／CSV 摄取、来源与绑定管理；普通 CLI 的真实 RAG 配置链仍不完整 |

本地知识摄取成功不代表普通对话能使用该索引。LangGraph 没有持久 checkpoint 或自动重放；
MCP 只加载用户名单中的工具，不提供并行任务编排。现有失败、未验证范围和历史验收分别记录在
[当前状态](docs/context/current.md)与[验证说明](docs/modules/verification.md)。

## 文档导航

- **使用**：[安装与操作](docs/guides/usage.md) · [配置参考](docs/reference/configuration.md) · [MCP](docs/guides/mcp.md) · [知识库](docs/guides/knowledge.md)
- **理解代码**：[架构总览](docs/architecture/overview.md) · [模块地图](docs/map.md) · [术语](docs/reference/glossary.md)
- **开发维护**：[开发指南](docs/development/contributing.md) · [Agent 入口](AGENTS.md) · [验证与验收](docs/modules/verification.md)
- **追溯历史**：[归档索引](docs/archive/README.md) · [架构决策](docs/decisions/README.md)

常规开发检查：

```powershell
uv run ruff check .
uv run ruff format --check .
uv run pyright
uv run pytest -q -m "not integration"
```

历史完整套件存在 RAG Recall 失败，具体 revision 和数字见[验证记录](docs/modules/verification.md)。
测试通过范围与真实服务、人工学习结果分别报告。
