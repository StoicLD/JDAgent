# MCP 配置与使用

状态：`Proposed`（配置、三类模型入口、结果处理及 CLI 目录／Task 管理已实施；完整 v0.7 仍在实施中）。

实现进度见[实施记录](../plans/v0.7-implementation.md)。当前 CLI 能读取并验证以下配置，普通对话不会因此启动 Server。
模型现在可通过 search_tools 检索已启用工具，并从下一请求开始调用选中的定义；两个 Runner 共用此路径。
完整定义还须装得进本次输入窗口；空间不足时在请求之间移除较少使用的定义，保留检索入口，不截断参数约束。
结果预览有界，完整原文可通过 read_mcp_result 按引用分页读回。
开启相应能力后，模型也可发现／读取 Resources，以及检索／获取 Prompts；用户也可直接用命令触发 Prompt。
新版 Tasks 已接入当前调用的顺序等待；CLI 支持重开会话后显式管理原任务。
TurnCoordinator 已提供本地状态、目录、显式刷新与 BM25 检索；两种 Runner 共用同一会话服务。
目录与索引的容量单位、默认值和固定检索样例见[实施记录](../plans/v0.7-implementation.md)。

## CLI 目录与原任务管理

| 命令 | 行为 |
| --- | --- |
| `/mcp status`（或 `/mcp`） | 查看本地连接、目录、协议版本、故障及活动操作的已用时间／进度，不发远端请求 |
| `/mcp list [server\|*] [offset]` | 发现并分页显示已启用能力及被拒绝能力的原因；已有目录可复用 |
| `/mcp refresh [server]` | 空闲时显式重连并刷新指定或全部 Server 的目录 |
| `/mcp tasks [offset]` | 分页显示当前 Session 已登记的本地任务引用和最后已知状态 |
| `/mcp task <task-ref> [query\|result\|cancel]` | 默认 query；按原身份查询、取回结果或取消，不重新提交业务工具 |
| `/mcp prompt <server> <name> [key=value ...]` | 获取明确指定的模板并交给当前任务；参数值含空格时加引号 |

目录和任务列表每页最多 50 项，按输出的 next_offset 继续读取；目录跨 Server 翻页时使用 `/mcp list * <offset>`。
长名称和 URI 显示截断标记，外部名称按字面显示。目录中的 rejected 项不能执行，并给出局部拒绝原因。
首次对话前也可发现目录：此时创建空 Session 保存所有权，不启动模型或伪造用户消息。
Task 操作使用列表中的本地引用，不接受远端私有句柄；先 `/resume <session>` 可管理原会话任务。
管理等待期间仍可查看本地状态和上下文窗口，使用 `/cancel` 停止本地等待，或 `/exit` 收尾退出。
`/mcp task <ref> cancel` 才是显式请求取消原远端 Task；停止本地管理等待不代表远端工作已停止。
其他管理操作与 `/compact` 均需空闲后重发，不排队。

直接触发示例：`/mcp prompt docs explain topic="MCP sessions"`。也可作为一次性输入：

```powershell
uv run jdagent --output json '/mcp prompt docs explain topic="MCP sessions"'
```

直接命令与模型的 get_mcp_prompt 共用目录、参数检查、获取和原文处理。交互时缺少必填参数，由正常对话
询问后再获取；Headless 返回退出码 4 和 mcp_prompt_invalid_arguments，并在回答字段列出缺失名称，不等待输入。
远端获取失败返回 tool_error 停止原因和具体 mcp_prompt_* 类别；用户取消仍返回 130。
用户的原始命令保留为用户事实，展开模板单独保存为外部材料，不伪造模型工具调用。
模板预览、压缩和历史读回继续保留来源，原文可用 read_mcp_result 分页读取；自动长期记忆排除模板及其派生内容。

## 用户定义 Server

在用户级 `config.toml` 中添加 `mcp` 表，文件位置沿用[CLI 配置约定](usage.md)。
Server ID 使用 1–64 个英文字母、数字、下划线或连字符，首字符是字母或数字。
Server 默认禁用，工具默认空名单，Resources／Prompts 默认关闭。工具名必须逐个写出，不能用 `*` 放行所有工具。
启用名单只选择已经人工确认过的查询、搜索、读取或计算工具；Server 的 readOnlyHint 不替代这个判断。

```toml
[mcp]
timeout_seconds = 600
result_quota_mib = 100

[mcp.servers.docs]
enabled = true
transport = "http"
url = "https://your-server.example/mcp"
bearer_env = "JD_DOCS_TOKEN"
tools = ["search", "read"]
resources = true
prompts = true
timeout_seconds = 1200

[mcp.servers.docs.tool_overrides.read]
timeout_seconds = 3600
safe_repeat = true

[mcp.servers.local_docs]
enabled = true
transport = "stdio"
command = "your-mcp-server"
args = ["--stdio"]
tools = ["search"]

[mcp.servers.local_docs.env]
API_TOKEN = "JD_LOCAL_DOCS_TOKEN"
```

上面的环境变量名是引用；实际值由用户在进程环境中提供，不能填进 TOML。公开 HTTP Server 可以省略 `bearer_env`。
不接受内嵌 Bearer 字段、URL 用户名／密码或项目级账号替换。HTTP `url` 支持 http／https；不支持 URL fragment。
stdio 的 env 表左边是交给 Server 的变量名，右边是宿主环境变量名。只按明确引用传值，并保留传输所需的最小默认环境。
配置读取只验证引用格式，连接时才读取值；缺失凭据应在对应 Server 的连接结果中报告，而不是阻止无关普通对话启动。

stdio 的 `cwd` 省略时使用当前工作区。显式相对路径相对于用户配置文件目录；绝对路径保持原位置。
`args` 按原数组传给进程，不经过 shell 字符串拼接。HTTP 不接受 stdio 的 command／args／cwd／env 字段，反向混用也拒绝。

等待预算按工具 > Server > mcp 默认值选择，默认 600 秒，必须为有限正数。连接 15 秒与收尾 5 秒是独立约束。
`mcp.result_quota_mib` 是每个 Session 的 MCP 结果存储配额，默认 100 MiB，必须为正整数。
只允许在用户配置中设置；空闲后的下一次操作重新读取配置，正在执行的操作保留开始时的限额。
降低配额不删除已有原文或改变既有发送事实；新增业务操作不能取得所需预留时，在发送前返回容量不足。
原文和私有控制对象按内容哈希计量，同一 Session 的相同对象只计一次；不同原文一直计入，不因再次查询替换旧结果。
每次保存结果的来源、预览、引用及结果字段按规范 JSON 的 UTF-8 字节计量；相同原文的多条结果记录仍分别占元数据容量。
发送前也预留这部分空间。发送意图、轮询、取消与统一 Session 封套属于会话控制日志，不从结果配额扣除；
普通模型转录和直接 Prompt 的材料投影也由既有 Session 存储管理，不重复计作 MCP 原文记录。
safe_repeat 默认为 false；设为 true 只允许宿主在可靠识别服务端业务超时且仍有时间／次数额度时考虑重复。
它不授权在客户端超时、断线、取消或结果不明后重发。当前普通工具调用已接通有条件重试：
可靠识别的 Server JSON-RPC 超时且 safe_repeat=true 时，总共最多 3 次，退避分别为 0.5 秒、1 秒。
首次调用、退避和重试共用 timeout_seconds，不会每次重置。HTTP 504、isError 或文本里的 timeout 均不足以重试。
同 Turn 失败后更换模型 call_id 不恢复已耗尽额度；成功结束后的正常再次读取仍可执行。
普通 MCP 调用不受内置工具的十秒限制。取消沿适配层的有界协议清理返回；已经收到的句柄与结果仍完成本地保存，
当前 Turn 按取消停止。范围与验证见[实施记录](../plans/v0.7-implementation.md)。

## 长操作与新版 Tasks

交互中可用 `/mcp status` 查看本地连接与目录状态，命令本身不会连接 Server。
活动操作还显示名称、类型、已用秒数和可用进度；底部状态栏每秒刷新一次本地快照。
进度没有总量时显示当前数值；未收到有效进度时显示 `progress=unavailable`，不代表执行失败。
Task 等待另显示最后已知状态。重试与轮询间隔计入同一次等待，进度通知不延长截止时间。
显示只保留最新的有效数值，不保存 Server 的进度消息正文，也不将进度注入模型上下文。
运行中仍可使用 `/status`、`/context-window`、`/mcp status` 和 `/cancel`；`/exit` 先取消并收尾再退出。
`/compact` 和刷新等操作需要等空闲后重新发出，不在当前 Turn 排队；审批期间这些命令不会被当成审批答案。

Server 协商新版 Tasks 后，可以把工具调用返回为任务。JDAgent 保存任务关联后顺序等待，
模型取得最终结果才继续；内部查询和重试不额外消耗业务工具次数，也不延长本次 timeout_seconds。
查询至少间隔 100 ms，Server 要求更长间隔时遵从该值。可识别的查询超时最多尝试三次，始终查询原任务。
显式管理也遵从最近保存的查询间隔，重启不会绕过；确认任务失败或已取消后释放未用的结果预留，未确认任务仍保留预留。
再次向 Server 取回结果时复用尚存的预留，或先持久登记新的结果预留；不足时不发送查询。
查询中断后预留跨重启保留，显式取回结果或确认无结果终态后释放。已保存的 result 读回不需要新额度。
额度不足仍可发送取消请求；此时跳过可能携带业务结果的后续查询，输出 result_query_skipped=capacity，
仅说明取消请求获接受，不声称远端已停止。提高配额后可再次显式查询。

用户取消或等待期限耗尽后，JDAgent 请求任务取消并查询状态，本地收尾等待至多五秒。
取消被接受和远端已停止分别记录；Server 仍报告 working 时，结果明确保持未确认。
Server 要求当前不支持的额外输入时，结束等待并请求取消。远端句柄不会出现在模型材料中，用户看到的是本地任务引用。
若取消期间收到最终结果，仍保留该结果原文。应用层允许按原会话的本地引用查询、读结果或再次取消；
每次远端操作核验原工作区、Server 和账号。换账号或端点不能查回原任务，stdio 新进程遗失任务时明确报错，不重新提交。
TTL 是 Server 保留任务的期限，更新后跨重启保留；本地已保存的结果仍可读回。完整恢复矩阵尚待验收。

## 模型使用资源和模板

相应开关启用后，模型可用以下工具；仅展示这些入口不会启动 Server。

| 工具 | 行为 |
| --- | --- |
| `list_mcp_resources` | 分页列出资源或 URI 模板，可指定 Server；只列目录，不读取内容 |
| `read_mcp_resource` | 向指定 Server 读取一个具体 URI，包括由资源模板展开的 URI |
| `search_mcp_prompts` | 用 BM25 检索 Prompt 候选，不自动获取模板 |
| `get_mcp_prompt` | 获取模型选中的模板；必填参数缺失时返回参数名，由模型继续正常对话澄清 |

URI 由 Server 解释，远端 `file:` URI 不进入本地文件工具。Resources 不写入 RAG。
Prompt 返回的消息角色保留在外部结果中；不会成为用户亲自输入的消息或系统指令。
这些模型调用均计入每轮工具调用限额，内部目录翻页不另计业务调用。

文本、文本嵌入资源和资源链接可以进入预览；资源链接不会隐式触发读取。
图片、音频和二进制资源的原文保留，模型预览只显示不支持标记。
混合结果显示处理不完整；完全没有受支持内容时返回不支持错误。
结果另外记录是否完整接收、Schema 校验结果及预览是否完整，避免把预览截断误认为接收失败。

结构化结果遵守协商版本：锁定的 SDK 2.3.0 对 `2026-07-28` 接受任意 JSON 值，
对 `2025-11-25` 的结构化字段接受对象或 null；旧协议下数组等值会报告协议失败。
已声明的输出 Schema 仅校验成功业务结果，且校验完整结构化数据；`isError=true` 保留为业务失败，
不要求它符合成功结果的 Schema。以上范围和运行证据见[实施记录](../plans/v0.7-implementation.md)。

## 项目只能收窄范围

项目 `.jdagent/config.toml` 只能引用用户已经定义的 ID。允许字段为 enabled、tools、resources、prompts：

```toml
[mcp.servers.docs]
tools = ["read"]
prompts = false

[mcp.servers.local_docs]
enabled = false
```

没有项目覆盖时继承用户设置；写空工具数组表示关闭该 Server 的全部工具。Resources 和 Prompts 分别由布尔开关控制，
不受 tools 数组替代。项目不能启用用户禁用的 Server／能力，也不能扩大工具名单；不能指定命令、URL、目录、环境、凭据或执行策略。
从项目名单中移除的工具，其超时／重复策略也不进入有效配置。

配置错误在创建会话前返回 CLI 配置错误，不输出 Token 值。配置文件变更不会授权重放已发送操作；
连接、认证身份和目录更新必须遵守空闲时刷新／重连的运行时合同。
