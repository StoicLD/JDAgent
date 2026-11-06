# MCP 配置与使用

状态：`Proposed`（配置、目录检索与普通工具调用已实施；完整 v0.7 能力和 CLI 管理命令仍在实施中）。

实现进度见[实施记录](../plans/v0.7-implementation.md)。当前 CLI 能读取并验证以下配置，普通对话不会因此启动 Server。
模型现在可通过 search_tools 检索已启用工具，并从下一请求开始调用选中的定义；两个 Runner 共用此路径。
结果预览有界，完整原文可通过 read_mcp_result 按引用分页读回。不要把此切片等同于 Resources／Prompts／Tasks 全部可用。
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
它不授权在客户端超时、断线、取消或结果不明后重发。总次数最多 3 次，实际执行规则仍由运行时实现和验收。
当前 P2a 已使用普通调用等待预算，但尚未启用有条件重试；safe_repeat 不会在此切片触发额外尝试。

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
