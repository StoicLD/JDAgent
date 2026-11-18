# MCP 连接、发现与执行

状态：`Implemented`（实现说明）。核对日期：2026-11-11，代码基准：`e23604f`。

MCP 是独立的远端能力接入层。它通过工具、Resources 和 Prompts 提供外部材料，不写入知识库。
配置和实际命令见[MCP 教程](../guides/mcp.md)；本文解释代码如何组织。

## 所有者与接口

| 代码 | 职责 |
| --- | --- |
| [mcp_configuration.py](../../src/jdagent/mcp_configuration.py) | 用户 Server 定义、工具名单、凭据引用与项目收窄 |
| [MCPService](../../src/jdagent/mcp_service.py) | 按 Session 管理连接、目录、BM25、定义缓存与本地活动状态 |
| [SDKMCPConnection](../../src/jdagent/adapters/mcp_connection.py) | 封装锁定 SDK，协商协议、调用传输和隔离单项畸形定义 |
| [mcp_transport.py](../../src/jdagent/adapters/mcp_transport.py)、[mcp_stdio.py](../../src/jdagent/adapters/mcp_stdio.py) | 有界 HTTP／stdio 传输与关闭 |
| [MCPTools](../../src/jdagent/mcp_tools.py)、[MCPMaterialTools](../../src/jdagent/mcp_material_tools.py) | 模型发现、动态工具、资源与 Prompt 入口 |
| [MCPExecution](../../src/jdagent/mcp_execution.py) | 发送意图、容量预留、尝试与结果持久化、预览与原文分页 |
| [MCPTaskExecution](../../src/jdagent/mcp_task_execution.py) | 当前调用内的顺序等待、轮询与取消 |
| [MCPTaskManagement](../../src/jdagent/mcp_task_management.py)、[MCPTaskBinding](../../src/jdagent/mcp_task_binding.py) | 重开后管理原任务、校验工作区／身份／代次与受控句柄 |
| [MCPDirectPrompts](../../src/jdagent/application/mcp_prompt.py) | 用户直接触发模板，保留用户命令与外部材料的区别 |
| [mcp_provenance.py](../../src/jdagent/mcp_provenance.py) | 外部来源及其派生关系跨压缩、恢复和历史检索传播 |

连接实现 MCPConnectionPort，不把 SDK 对象和秘密泄漏到领域事件。
Server 配置不会在普通启动时立即连接；目录发现或业务操作按需建立连接，`/mcp status` 只读本地状态。

## 从检索到模型调用

1. 用户明确启用 Server 和工具名单；项目只能收窄。Resources 与 Prompts 有独立开关。
2. 模型通过 `search_tools` 触发目录发现，以名称／描述的 BM25 分数选工具，不调用 Embedding 或 RAG。
3. 选中的完整定义进入第二层缓存，从下一次模型请求起生效。工具名带宿主路由，原 Server／名称另存。
4. ToolRegistry 为请求冻结定义快照；执行时核对实际曝光定义、Schema、目录代次和当前授权。
5. 发送前记录调用意图与容量预留，接收后保存完整结果对象和持久事实，再向模型返回有界预览。
6. 模型用 `read_mcp_result` 读取当前会话已发布的原文引用，按 Unicode 字符分页，每页最多 2048 字符。

目录缓存默认最多 2000 项、8 MiB 元数据、4 MiB 检索索引，每类能力最多 20 页。
模型定义缓存默认最多 16 项、8192 估算 token；仍受完整请求预算约束，空间不足时整体移除较少使用的定义，
不截断 Schema。默认结果配额每 Session 100 MiB，由用户 `mcp.result_quota_mib` 配置。
默认单次预留为 2 MiB + 16 KiB，预览上限 12000 估算 token，并受请求预算进一步约束。
常量分别见 [domain/mcp.py](../../src/jdagent/domain/mcp.py)、[mcp_definitions.py](../../src/jdagent/mcp_definitions.py)、MCPExecution。

单项非法定义或不支持的 Schema 被局部拒绝，合法同级项仍可发现；损坏的响应信封则使目录准备失败。
不完整目录、空搜索结果和服务失败有不同的输出，不把部分结果描述为完整覆盖。

## 超时、重试与 Task

普通 MCP 调用使用独立等待期限：工具覆盖 > Server > mcp，默认 600 秒；不受内置工具默认十秒限制。
只有可靠识别的服务端业务超时且 `safe_repeat=true` 时，普通调用才最多尝试三次，退避 0.5／1 秒，
所有尝试共用原截止时间。断线、客户端超时、HTTP 504 或结果不明不自动重发。

Task 返回后先持久保存受控句柄关联，再顺序查询原任务；查询和等待不额外计业务工具次数。
进度只更新显示，不延长期限。取消时请求远端取消并有界查询结果；收到最终结果仍保存。
本地取消、取消请求获接受、远端已停止是不同事实。重启后通过本地 task_ref 显式管理，不重新提交工具。
私有句柄不能由普通原文入口读回；身份或代次不符时拒绝远端管理。

结果配额同时计内容对象和每次结果元数据，相同对象按哈希去重；不同结果继续累计。
未确认结果的持久预留跨重启保留，容量不足时新增调用在发送前拒绝。详细规则见[MCP 教程](../guides/mcp.md)。

## 外部材料与恢复

文本、资源链接和文本嵌入资源可进入预览，链接不自动读取。图片、音频和二进制原文保存，但模型预览标记不支持。
完整接收、输出 Schema 校验、受支持内容和预览截断分别记录。
直接 Prompt 的原命令是真实用户输入，展开内容单独保存为 `mcp_prompt_prepared`，模板角色不提升为系统权限。

MCP 与派生内容在压缩摘要、恢复子会话和历史读回中保留宿主来源标记；自动记忆提取排除这些材料。
发送意图、Task 登记和最终结果使用同一 Session 事件链；截断恢复不猜测未发布的引用，不自动重提业务调用。

## 验证入口

[配置](../../tests/test_v07_mcp_configuration.py)、[目录](../../tests/test_v07_mcp_catalog.py)、
[模型工具](../../tests/test_v07_mcp_model_tools.py)、[资源与 Prompt](../../tests/test_v07_mcp_resources_prompts.py)、
[配额](../../tests/test_v07_mcp_result_quota.py)、[Task 管理](../../tests/test_v07_mcp_task_management.py)、
[进程恢复](../../tests/test_v07_mcp_process_recovery.py)、[SDK 互操作](../../tests/test_v07_mcp_sdk_interop.py)。
阶段测试映射已[归档](../archive/2026-11-11/development/v0.7-acceptance-matrix.md)，
历史结论见[实现审查](../reviews/REV-20261106-003-v0.7-implementation.md)。
