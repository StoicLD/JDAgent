# JDAgent 安装与使用教程

状态：`Implemented`（当前操作说明），更新：2026-11-05。

按当前包版本 0.5.0 及 v0.6 可选后端实现核对，面向 Windows PowerShell。`powershell` 代码块在终端执行，`text` 中的
命令和对话示例在 JDAgent 交互界面输入。尖括号中的 ID 是占位符，使用前替换为实际值。

## 安装与更新

本机已有仓库时，进入产品目录：

```powershell
Set-Location 'E:\Projects\Codex_Projects_Overall\JDAgent\JDAgentProject'
```

新电脑需要 Git、Python 3.11+ 和 uv。已有 Python、尚无 uv 时可执行：

```powershell
python -m pip install uv
uv --version
```

若找不到 `uv`，检查 Python Scripts 是否在 PATH 中，或用 `python -m uv` 替代下文的 `uv`。
首次获取仓库：

```powershell
git clone https://github.com/StoicLD/JDAgent.git JDAgentProject
Set-Location JDAgentProject
```

远端只包含已推送提交；本机未推送的实现不会出现在新克隆中。安装当前检出的版本：

```powershell
uv sync --locked
uv run jdagent --version
uv run jdagent --help
```

`uv sync --locked` 根据锁文件安装运行和开发依赖，在 `.venv` 建立环境，无需手动激活。
仓库指定 Python 3.11.15，首次安装可能联网下载解释器和依赖。取得新版代码后再次执行
`uv sync --locked`；离线安装只能使用已缓存依赖。当前预期版本为 `jdagent 0.5.0`。
也可直接运行 `.\.venv\Scripts\jdagent.exe --version`，排除使用了其他环境的情况。

一般功能无需 rag 扩展。Milvus 相关依赖可用 `uv sync --locked --extra rag` 安装；
这不会启动 Milvus 服务，也不会补齐[知识库配置缺口](knowledge.md)。文件编辑和命令执行
使用 Windows 实现，不应把 Python 能安装等同于全部工具跨平台可用。

## 离线演练与真实对话

```powershell
uv run jdagent --provider fake --output json "离线测试"
uv run jdagent --provider fake
```

第一条返回单次 JSON；第二条进入交互界面。输入一条普通消息，再尝试 `/status`、`/trace`、
`/sessions`，最后 `/exit`。Fake 重放预设响应，不理解任务，不能证明真实文件任务或记忆质量。

真实模型默认使用 DeepSeek，模型标识 `deepseek-v4-flash`，端点 `https://api.deepseek.com`。
这是程序默认值，不保证账号始终有对应模型访问权。在当前 PowerShell 设置 Key：

```powershell
$env:DEEPSEEK_API_KEY = [System.Net.NetworkCredential]::new('', (Read-Host 'DeepSeek API Key' -AsSecureString)).Password
uv run jdagent --provider deepseek --tool-scope analyze
```

提示输入避免把 Key 明文写进命令历史；变量仅在当前终端及其子进程生效。
用完可执行 `Remove-Item Env:DEEPSEEK_API_KEY`。程序不会自动读取 `.env`。
另一方式是在 `%APPDATA%\JDAgent\config.toml` 指定用户密钥文件：

```toml
provider = "deepseek"
model = "deepseek-v4-flash"
api_key_file = 'C:\Users\YourName\.secrets\deepseek-key.txt'
```

先自行建立密钥文件，替换用户名，文件内容为 Key。密钥优先级为环境变量、用户配置的
`api_key_file`、开发期兼容路径 `../tmp/keys/deepseek-api-key.txt`。新安装无需创建兼容文件。
`api_key_file` 不能放进项目配置。

## 选择执行引擎与模型适配器

v0.6 的新后端可以显式试用，默认仍是 native/native。执行引擎负责模型与工具之间的调度，模型
适配器负责请求与流的转换；下面四种组合都可选择，其他命令、权限和数据位置沿用原行为。

| 组合 | CLI 参数 | 用途 |
| --- | --- | --- |
| 原生执行与原生接入 | `--runtime-engine native --model-adapter native` | 当前默认及回退入口 |
| 原生执行与 LangChain 接入 | `--runtime-engine native --model-adapter langchain` | 单独试模型适配器 |
| LangGraph 执行与原生接入 | `--runtime-engine langgraph --model-adapter native` | 单独试图调度 |
| LangGraph 执行与 LangChain 接入 | `--runtime-engine langgraph --model-adapter langchain` | v0.6 目标组合 |

例如，已按上一节设置 DeepSeek Key 后启动目标组合：

```powershell
uv run jdagent --provider deepseek --runtime-engine langgraph --model-adapter langchain --tool-scope analyze
```

仅做离线启动检查：

```powershell
uv run jdagent --provider fake --runtime-engine langgraph --output json "离线测试"
```

Fake 总是使用固定脚本，不能用它证明 LangChain 的网络接入、真实问答或压缩质量通过。
两项选择也可放在 `%APPDATA%\JDAgent\config.toml` 的顶层；不能放项目 `.jdagent/config.toml`：

```toml
runtime_engine = "langgraph"
model_adapter = "langchain"
```

切换任一后端必须退出并重新启动，CLI 可覆盖用户配置。model、timeout、输出上限等仍按每轮重读，
已经建立的请求保留自己的参数。回退时显式传 native/native，再用原来的 `/resume <名称或ID>`
入口继续会话；遇到 unknown 副作用仍需按恢复提示核查，后端切换不使未知结果自动变成已完成。
当前两个后端共用 v0.5 数据格式，不需要清空会话。版本验收和已知限制见[v0.6 实施记录](../plans/v0.6-implementation.md)。

## 工作区、文件操作与权限

保持终端位于产品仓库，通过 `--workspace` 指向已有目标项目，按需要执行其中一条：

```powershell
uv run jdagent --workspace 'E:\Projects\Example' --tool-scope analyze
uv run jdagent --workspace 'E:\Projects\Example' --tool-scope edit
uv run jdagent --workspace 'E:\Projects\Example' --tool-scope execute --command-permission ask
```

| 范围 | 能力 | 示例请求 |
| --- | --- | --- |
| analyze | 计算、查找、读取、搜索、读取命令日志、查询历史与记忆 | 查找配置解析代码并解释优先级，不修改文件。 |
| edit | 包含 analyze，增加创建、精确编辑及获授权记忆修改 | 读取 prices.py，修正求和逻辑，只修改这个文件。 |
| execute | 包含 edit，增加执行明确程序与参数 | 修改后用项目 Python 运行 verify.py，报告实际退出码。 |

默认范围为 execute。文件写入和命令默认 ASK，审批会展示目标、差异或完整命令参数。
按界面提示决定；Session 允许规则会保存，用 `/permissions` 查看，
`/permissions revoke <rule-id>` 撤销。用户或项目 DENY 不能靠会话规则放宽。
`write_permission = "deny"` 同时禁止命令执行；`command_permission = "deny"` 仅禁止命令。

文件工具处理 workspace 内普通 UTF-8 文件，读取和编辑上限为 2 MiB。编辑前需读取最新
revision；出现冲突时让 Agent 重新读取。命令采用明确程序与参数，不隐式解释 Shell
管道、重定向或 bat/cmd。程序找不到时提供可执行文件完整路径。
命令在受信任本机运行，不是不可信代码沙箱。详细范围见[工具模块](../modules/tools.md)。

## 保存、切换和恢复会话

首次发送消息后才有当前 Session。进入界面尚未发言时，部分命令会提示没有当前会话。

```text
/rename 配置梳理
/sessions
/new
请解释这个项目的目录结构。
/resume 配置梳理
```

`/new` 开始新对话，原会话仍保留。名称含空格时，恢复可直接使用 `/sessions` 列出的 ID。
`/exit` 后，下次启动用 `/resume <名称或ID>` 继续，也可从 PowerShell 指定：

```powershell
uv run jdagent --workspace 'E:\Projects\Example' --session-id '实际SESSION_ID'
uv run jdagent --workspace 'E:\Projects\Example' --session-id '实际SESSION_ID' "继续解释刚才的方案"
```

恢复时沿用 workspace 和数据目录。身份取决于规范绝对路径，移动项目或换克隆不会自动继承
原会话。正常会话可恢复允许规则；异常恢复若创建子会话，不继承权限，也不自动重放结果
未知的副作用。遇到未知执行提示，先核对实际文件或命令结果。v0.5 不兼容旧 Session 格式，
不要通过改目录名把旧 JSONL 冒充新格式。

## 交互命令速查

| 输入 | 用途 |
| --- | --- |
| `/help` | 命令帮助 |
| `/status` | 当前会话与运行状态 |
| `/new` | 开始新会话 |
| `/sessions` | 列出当前工作区会话 |
| `/resume <名称或ID>` | 恢复指定会话，参数不可省略 |
| `/rename <名称>` | 命名当前会话 |
| `/permissions` | 列出会话允许规则 |
| `/permissions revoke <rule-id>` | 撤销指定规则 |
| `/trace` | 执行轨迹摘要 |
| `/knowledge status` | 知识绑定数量 |
| `/knowledge bindings` | 工作区绑定列表 |
| `/knowledge sources` | 绑定知识库的来源列表 |
| `/logs clear` | 清理当前会话命令输出，保留清理标记 |
| `/compact` | 提前请求压缩 |
| `/context-window` | 窗口、输入预算、最近完整请求占用与分类 |
| `/exit` | 退出并保留会话 |

上表包含同一命令的多种用法，命令名共 13 个。没有 `/context`、`/memory`，也没有独立的
`/logs` 浏览命令。要读日志，可要求 Agent 用 `read_command_output` 读取工具返回的日志 ID。
运行中 Ctrl+C 取消本轮；空闲时 Ctrl+C 退出，返回 130。运行中 `/compact` 排队，重复请求
合并；审批期间控制命令不会成为审批答案。

## 上下文、压缩与目录规则

`/context-window` 展示最近完整请求的估算占用和快照时间，分类包括用户内容、系统指令、
工具定义、历史摘要等。它不是累计费用；尚无请求时也不能把空快照当成模型实际占用为零。
默认达到输入预算的 90% 后压缩，40% 是目标。`/compact` 可提前触发；执行中需等待已发出
工具批次完成。压缩只调整模型下次看到的历史，磁盘原文保留。

受保护原文太大时，查看最大的条目，缩短新输入，或明确撤销已失效的约束。
例如 `撤销本会话约束 C12`，ID 必须取自实际提示。这不会删除磁盘原文。

```powershell
uv run jdagent --max-context-tokens 128000 --max-output-tokens 4096
```

这里设置应用上限和输出预留；应用上限不能扩大模型真实容量。未知模型或自定义端点还需
通过 `--model-context-window` 填实际容量，不要用任意大的数值代替服务能力确认。

项目规则放在 workspace 的 `AGENTS.md`，子目录可有自己的规则。Agent 访问目录时查看
适用祖先链；首次写入遇到新规则时先返回规则，再由模型重新发起写入。
规则正文在本轮固定，修改通常下一轮生效。全局规则通过 `global_instructions` 配置文件路径。

## 历史检索与长期记忆

使用真实模型，在同一 workspace 输入：

```text
搜索之前会话中关于配置优先级的讨论，读取原文并给出来源会话与位置。
查找本项目关于测试工具的记忆，列出记忆 ID、版本和状态。
记住：项目使用 pytest
记为我的通用偏好：回答使用中文
```

历史检索使用 `search_history`、`read_history`；记忆查询使用 `search_memory`。
记忆修改通过模型工具执行，以工具返回的 ID 和状态为准，不以一句“记住了”作为保存证据。
直接意图在 ASK 下不需再次确认，但 analyze 或写入 DENY 仍会阻止修改。
取得实际记忆 ID 后，再输入以下格式，替换占位符：

```text
把本项目记忆 <实际记忆ID> 更正为：项目使用 pytest，测试位于 tests 目录
忘记本项目记忆 <实际记忆ID>
重新启用本项目记忆 <实际记忆ID>
```

用户级操作将“本项目记忆”替换为“我的通用偏好”。忘记表示停用，原文和旧版本保留；
更正停用内容不会自动启用。可明确查询停用记录。新增或更正正文末尾可写期限：
`[有效至：2026-12-01T00:00:00+08:00]`。引用或分析文本中的“记住”不产生直接授权。

新会话首个成功 Turn 后，后台从本项目稳定的旧会话整理项目记忆，当前会话和活跃来源不参与
该次提取。不是每轮都整理，也不保证首轮就拿到本次新生成的记忆。
没有 Embedding 配置或服务失败时使用关键词检索，标为 `keyword_only`。
独立的语义配置见[记忆模块](../modules/memory.md#配置示例)，密钥变量为
`JDAGENT_MEMORY_EMBEDDING_API_KEY`；它不会自动使用 DeepSeek 对话 Key。

```powershell
uv run jdagent maintain --pending --workspace 'E:\Projects\Example'
```

`maintain` 必须放在首个位置，不能带 Prompt 或 `--session-id`。它续做已有作业，不新建会话，
不强制为全部历史新建作业；执行可能调用模型。超出单个维护输入预算的历史会话目前尚未
分块提取，相关作业可能失败。完整边界见[实现记录](../reviews/REV-20261022-001-v0.5-implementation.md)。

## 配置与数据位置

目标 workspace 的 `.jdagent/config.toml` 使用 TOML。下面列出常用默认值，可选择性合并到
已有配置，避免覆盖自己的选项。`[memory]` 之后的字段属于该表。

```toml
model = "deepseek-v4-flash"
tool_scope = "execute"
write_permission = "ask"
command_permission = "ask"
model_timeout_seconds = 30
tool_timeout_seconds = 10
max_context_tokens = 64000
max_output_tokens = 4096
compact_trigger_ratio = 0.9
compact_target_ratio = 0.4
max_model_calls = 16
max_tool_calls = 32
max_failure_recoveries = 2

[memory]
auto_consolidation = true
headless_drain_seconds = 120
input_token_limit = 10000000
output_token_limit = 1000000
total_input_token_limit = 30000000
total_output_token_limit = 3000000
```

普通配置优先级为 CLI > 项目 > 用户 > 默认值；DENY 另有不能放宽的限制。
`provider`、`base_url`、`api_key_file`、`runtime_engine`、`model_adapter` 只能放用户配置，
除 `api_key_file` 外都可用 CLI 覆盖。未知或不允许的字段会报错。
每轮开始重读配置；Provider、端点或上述后端选择变化应重启。
`max_model_calls` 等是任务次数限制，不是模型容量。

`memory.input_token_limit` / `output_token_limit` 限制维护费用，`total_*` 限制含普通请求在内
的总费用，单位都是 token。费用持久累计，重启和新建会话不会清零。
`auto_consolidation = false` 关闭自动整理；`headless_drain_seconds = 0` 不在单次回答后等待
整理，待办仍可留待后续启动或 maintain。

| Windows 默认位置 | 内容 |
| --- | --- |
| `%APPDATA%\JDAgent\config.toml` | 用户配置 |
| `<workspace>\.jdagent\config.toml` | 项目配置 |
| `%LOCALAPPDATA%\JDAgent\projects\sha256-…\sessions-v5` | 本项目会话和 Context 对象 |
| `%LOCALAPPDATA%\JDAgent\projects\sha256-…` | 工作区身份、会话索引、输入历史等 |
| `%LOCALAPPDATA%\JDAgent\memory-v5.sqlite` | 默认记忆、作业和费用账本 |
| `%LOCALAPPDATA%\JDAgent\knowledge` | 知识 Catalog、原文对象等 |

`--data-dir` 改变 Session 目录，自定义目录时记忆数据库也放在那里；它不是改变所有配置、
知识和索引根目录的总开关。恢复时沿用相同参数。备份前退出相关 JDAgent 进程，保留完整
相关数据目录；只复制会话 JSONL 不能带齐对象或父会话引用。

## 单次运行与脚本调用

```powershell
uv run jdagent --tool-scope analyze "解释这个项目的启动流程"
uv run jdagent --tool-scope analyze --output json --show-trace "解释这个项目的启动流程"
$LASTEXITCODE
```

带 Prompt 时只运行一轮。JSON 合同为 v2，含回答、停止原因、用量、知识状态、引用和输出
时刻的 maintenance 状态。先输出回答，再可能等待后台维护，默认最多 120 秒；已输出的 JSON
不会随后改写。Headless 不弹交互审批，ASK 操作不会自行获准；需要人工确认时用交互模式。

退出码：正常结束 0，内部错误 1，用法/配置错误 2，会话错误 3，运行错误 4，取消 130。
退出 0 不等于任务独立验收通过，还应查看实际工具结果和停止原因。

## 常见问题

| 现象 | 处理 |
| --- | --- |
| 缺少 Key、认证失败 | 核对当前终端变量或用户密钥文件；不要在问题报告中粘贴 Key |
| workspace 不存在 | 选择已有目录，含空格路径加引号 |
| 没有当前会话 | 先发送普通消息或 `/resume` 已有会话 |
| 旧会话或记忆不可见 | 核对规范路径、`--data-dir` 和格式版本 |
| Session 被占用 | 关闭使用它的进程，不靠删锁文件绕过所有权 |
| 写入或命令被拒绝 | 查看范围、DENY、审批决定；Headless 不等人工批准 |
| revision 冲突 | 重新读文件再修改，不用旧版本重复覆盖 |
| 压缩后仍超限 | 查看占用与保护条目，缩短新输入、撤销失效约束或合理调整上限 |
| 费用预算不足 | 核对累计账本和限额，减少任务或明确提高预算，不靠重启清账 |
| 回答后暂未退出 | 检查后台等待，必要时调整 headless_drain_seconds |
| 只有关键词结果 | 查看 keyword_only，核对 Embedding 服务、模型、维度和专用密钥 |
| 知识导入成功但问答不使用 | 查看[知识库教程](knowledge.md)，导入不代表真实检索已接通 |

开发检查见[验证与验收](../modules/verification.md)。固定三次 DeepSeek 通过不代表完整故障
矩阵、语义质量、RAG Recall 或人工学习出口均已通过。

本文验证记录（2026-10-23）：已在隔离临时工作区运行安装后的 0.5.0 CLI，确认 Fake JSON
返回 v2，并通过 TOML 示例解析、项目配置加载与所列交互命令解析。未为文档更新调用真实
模型，也未把已有环境验证写成全新机器安装验证。

2026-11-05 补充后端选择：四组合参数解析、配置限制、请求重载、文件效果和恢复证据见
[实施记录](../plans/v0.6-implementation.md)。本次教程更新核对了 CLI 参数、TOML 示例和文档链接，
没有执行付费 Live，也没有把开发环境检查写成全新机器安装验收。

同日后续真实验证另获授权并完成[固定六次对照](../reviews/v0.6-live-comparison-20261105-r2.md)：
任务检查 6/6 通过，目标组冻结比较 1/3 通过。因此新后端继续作为显式选项，默认不切换；
独立评审、完整发布和本人学习验收仍未完成。
