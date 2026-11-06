# MCP 配置与使用

状态：`Proposed`（配置、三类能力的模型入口与结果处理已实施；完整 v0.7 和 CLI 管理命令仍在实施中）。

实现进度见[实施记录](../plans/v0.7-implementation.md)。当前 CLI 能读取并验证以下配置，普通对话不会因此启动 Server。
模型现在可通过 search_tools 检索已启用工具，并从下一请求开始调用选中的定义；两个 Runner 共用此路径。
完整定义还须装得进本次输入窗口；空间不足时在请求之间移除较少使用的定义，保留检索入口，不截断参数约束。
结果预览有界，完整原文可通过 read_mcp_result 按引用分页读回。
开启相应能力后，模型也可发现／读取 Resources，以及检索／获取 Prompts；直接 Prompt 命令和 Tasks 仍待接通。
TurnCoordinator 已提供本地状态、目录、显式刷新与 BM25 检索；两种 Runner 共用同一会话服务。
目录与索引的容量单位、默认值和固定检索样例见[实施记录](../plans/v0.7-implementation.md)。

## 用户定义 Server

在用户级 `config.toml` 中添加 `mcp` 表，文件位置沿用[CLI 配置约定](usage.md)。
Server ID 使用 1–64 个英文字母、数字、下划线或连字符，首字符是字母或数字。
Server 默认禁用，工具默认空名单，Resources／Prompts 默认关闭。工具名必须逐个写出，不能用 `*` 放行所有工具。
启用名单只选择已经人工确认过的查询、搜索、读取或计算工具；Server 的 readOnlyHint 不替代这个判断。

```toml
[mcp]
timeout_seconds = 600

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
safe_repeat 默认为 false；设为 true 只允许宿主在可靠识别服务端业务超时且仍有时间／次数额度时考虑重复。
它不授权在客户端超时、断线、取消或结果不明后重发。当前普通工具调用已接通有条件重试：
可靠识别的 Server JSON-RPC 超时且 safe_repeat=true 时，总共最多 3 次，退避分别为 0.5 秒、1 秒。
首次调用、退避和重试共用 timeout_seconds，不会每次重置。HTTP 504、isError 或文本里的 timeout 均不足以重试。
同 Turn 失败后更换模型 call_id 不恢复已耗尽额度；成功结束后的正常再次读取仍可执行。
Tasks、进度及应用级有界清理仍在实施，范围与验证见[实施记录](../plans/v0.7-implementation.md)。

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
