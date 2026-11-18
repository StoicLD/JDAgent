# 配置参考

状态：`Implemented`。核对日期：2026-11-11，代码基准：`e23604f`。
字段以 [configuration.py](../../src/jdagent/configuration.py)、CLI 参数以 [cli.py](../../src/jdagent/cli.py) 为准。
首次运行见[使用教程](../guides/usage.md)，MCP 的嵌套结构见[MCP 教程](../guides/mcp.md)。

## 文件与优先级

Windows 用户配置为 `%APPDATA%\JDAgent\config.toml`，项目配置为 `<workspace>\.jdagent\config.toml`。
文件可不存在；未知字段、错类型或项目不允许的字段会在启动校验时报错。
普通字段按 **CLI > 项目 > 用户 > 默认值** 解析；`memory` 表按层整体替换，不逐键合并。
需要在项目中覆盖 memory 时，应一起填写要保留的用户级选项。

用户或项目 `write_permission`／`command_permission` 为 deny 时，最终仍为 deny。
项目显式把用户 deny 改成 ask 会报配置错误。Session 允许规则也不能放宽 deny。
MCP 有专门的逐 Server 收窄规则，不使用普通覆盖逻辑。

## 顶层字段

“用户”表示仅用户 TOML；“两层”表示用户和项目 TOML 均可设置。CLI 仅支持列出的选项。

| 字段 | 默认值 | TOML 范围 | CLI |
| --- | --- | --- | --- |
| `provider` | `deepseek`（也支持 `fake`） | 用户 | `--provider` |
| `model_adapter` | `native`（也支持 `langchain`） | 用户 | `--model-adapter` |
| `runtime_engine` | `native`（也支持 `langgraph`） | 用户 | `--runtime-engine` |
| `model` | `deepseek-v4-flash` | 两层 | `--model` |
| `base_url` | `https://api.deepseek.com` | 用户 | `--base-url` |
| `api_key_file` | 未设置，使用开发期兼容路径作为最后回退 | 用户 | 无 |
| `model_timeout_seconds` | 30 | 两层 | `--model-timeout-seconds` |
| `tool_timeout_seconds` | 10，适用于内置工具，MCP 单独计时 | 两层 | `--tool-timeout-seconds` |
| `tool_scope` | `execute`（analyze／edit／execute） | 两层 | `--tool-scope` |
| `write_permission` | `ask`（也支持 deny） | 两层 | 无 |
| `command_permission` | `ask`（也支持 deny） | 两层 | `--command-permission` |
| `max_context_tokens` | 64000，应用窗口上限 | 两层 | `--max-context-tokens` |
| `model_context_window` | 未设置，使用已知模型档案 | 两层 | `--model-context-window` |
| `max_output_tokens` | 4096 | 两层 | `--max-output-tokens` |
| `context_safety_tokens` | 未设置，由 ContextBudget 计算 | 两层 | 无 |
| `provider_input_limit` | 未设置 | 两层 | 无 |
| `compact_trigger_ratio`／`compact_target_ratio` | 0.9／0.4 | 两层 | 无 |
| `global_instructions` | 未设置，显式规则文件路径 | 两层 | 无 |
| `instructions_max_tokens` | 未设置 | 两层 | 无 |
| `max_model_calls`／`max_tool_calls` | 16／32，每轮上限；普通 CLI 的覆盖限制见下文 | 两层 | 无 |
| `max_failure_recoveries` | 2，第三次观察到失败时停止；普通 CLI 的覆盖限制见下文 | 两层 | 无 |
| `fake_delay_seconds` | 0，仅 fake 可非零 | 两层 | `--fake-delay-seconds` |

`--workspace`、`--session-id`、`--data-dir`、`--output text|json`、`--show-trace` 是 CLI 入口参数，
不能直接写成 TOML 顶层字段。`--output` 需要同时提供单次 Prompt。
普通 CLI 没有 `--write-permission`；通过 TOML 管理文件写权限。

**现有接线限制**：上述三个调用／失败次数字段可以被配置解析器接受，但普通 CLI 的
`host.run_cli()` 总会传入 `RuntimeOptions`，组合根因此固定使用 `16 / 32 / 2`，覆盖这三个配置值。
直接调用 `build_runtime()` 且不传 RuntimeOptions 时，才在每轮读取配置中的次数。
这不是调整窗口或费用限额的限制。已用隔离 Fake 探针确认，见[核对记录](../development/documentation-refresh-2026-11-11.md)。

比例须满足 `0 < target < trigger < 1`。容量与次数字段要求正整数，安全余量可为零；
超时须为有限正数。未知模型或自定义端点需提供真实 `model_context_window`，应用上限不会扩大服务容量。

## 记忆与费用

以下字段位于 `[memory]`，两层 TOML 均可配置，暂无对应 CLI 参数：

| 字段 | 默认值 | 含义 |
| --- | --- | --- |
| `auto_consolidation` | true | 首轮成功后自动整理历史 |
| `headless_drain_seconds` | 120 | 输出单次回答后等待维护的上限，0 表示不等 |
| `input_token_limit`／`output_token_limit` | 10000000／1000000 | 维护请求累计 token 限额 |
| `total_input_token_limit`／`total_output_token_limit` | 30000000／3000000 | 包含前台请求的总累计 token 限额 |

这些是 token 数，不是货币金额，持久累计，不随新会话或重启归零。
`[memory.embedding]` 必须同时提供 `base_url`、`model`、正整数 `dimension`；
密钥来自 `JDAGENT_MEMORY_EMBEDDING_API_KEY`。不配置或调用失败时退回关键词检索。
这组字段不会配置 RAG 的 Embedding。

## 密钥与路径解析

DeepSeek 按 `DEEPSEEK_API_KEY` → 用户 `api_key_file` → 开发期 `../tmp/keys/deepseek-api-key.txt` 查找。
最后一项是代码的既有兼容路径，不是启动文档来源，也不是新安装要求；不自动读取 `.env`。
相对 `api_key_file` 相对于用户配置文件目录；`global_instructions` 相对于启动进程目录解析。

stdio MCP 的相对 cwd 相对于用户配置目录，省略 cwd 使用 workspace。MCP 凭据只存环境变量名引用，
连接时才取实际值。其他 MCP 超时、配额及项目限制见[MCP 配置](../guides/mcp.md#用户定义-server)。

## 配置何时生效

Turn 开始重读配置；当前轮的指令和已建立请求保持快照。模型名、预算、工具范围等在下一轮生效。
Provider、base_url、执行引擎、模型适配器或记忆 Embedding Profile 变化需要重启。
MCP 配置和结果配额在空闲后的下一次相关操作重读；正在执行的操作保留开始时限额。

## 数据位置

路径计算见 [data_paths.py](../../src/jdagent/data_paths.py)，记忆位置由[组合根](../../src/jdagent/composition.py)确定。

| Windows 默认位置 | 数据 |
| --- | --- |
| `%APPDATA%\JDAgent\config.toml` | 用户配置 |
| `<workspace>\.jdagent\config.toml` | 项目配置 |
| `%LOCALAPPDATA%\JDAgent\projects\sha256-…` | 工作区清单、会话发现索引、输入历史 |
| 上述目录的 `sessions-v5` | 会话 JSONL、Context／MCP 对象、命令输出 |
| `%LOCALAPPDATA%\JDAgent\memory-v5.sqlite` | 默认记忆、意图、维护作业与费用 |
| `%LOCALAPPDATA%\JDAgent\knowledge` | Catalog、内容对象、备份与本地索引 |

`--data-dir` 仅指定 Session 目录；自定义目录时，Memory SQLite 也在该目录。
用户配置、工作区发现索引、输入历史和知识根目录仍按 DataPaths 计算。恢复会话时沿用相同参数。
工作区身份来自规范绝对路径，移动目录或换克隆不会自动继承原数据。

数据路径也有 macOS/Linux 分支，但这不表示 Windows 文件／命令工具已跨平台实现。
备份前关闭相关进程，保留完整会话目录、对象、所引用父会话及所需的 Memory／Knowledge 数据；
仅复制 JSONL 不足以恢复全部原文。旧会话不自动导入、迁移或删除。
