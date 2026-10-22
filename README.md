# JDAgentProject

状态：`Implemented`（项目说明）

JDAgent 是一个以实践学习 Agent 核心机制的 Python 项目，先建设通用 Runtime，
后续再通过独立领域包增加求职能力。当前已有模型／工具循环、持久 CLI、RAG P1 和
八工具执行层，以及 v0.5 上下文压缩、跨会话检索和记忆；工程与学习门禁分别记录。

从[文档 Map](docs/map.md)按模块了解当前实现；
[当前状态](docs/context/current.md)记录待办与门禁，
[验证与验收](docs/modules/verification.md)提供原始证据入口。
包版本为 `0.5.0`，不兼容旧 Session；本版本不会再次删除历史目录。

## 快速开始

需要 Python 3.11+ 和 uv，在产品仓库根目录执行：

```powershell
uv sync
uv run jdagent
```

无 Prompt 时进入交互模式，`/help` 查看命令，`/sessions` 查看会话，
`/resume <名称或ID>` 恢复，`/exit` 退出并保留会话。
默认使用 DeepSeek；离线体验显式选择 Fake：

```powershell
uv run jdagent --provider fake
uv run jdagent --provider fake --output json "离线测试"
```

单次请求、JSON 输出和工作区工具范围示例：

```powershell
uv run jdagent "解释 ModelPort"
uv run jdagent --output json --show-trace "解释 ModelPort"
uv run jdagent --session-id <SESSION_ID> "继续"
uv run jdagent --workspace E:\Projects\Example --tool-scope analyze
uv run jdagent --workspace E:\Projects\Example --tool-scope edit
uv run jdagent --workspace E:\Projects\Example --tool-scope execute --command-permission ask
```

DeepSeek Key 优先取 `DEEPSEEK_API_KEY`，其次取用户配置的 `api_key_file`，
最后回退开发文件 `../tmp/keys/deepseek-api-key.txt`。完整配置、13 条交互命令和
数据位置见[CLI 与配置](docs/modules/cli.md)。

Windows 八工具支持文件发现、读取、搜索、创建、精确编辑及命令执行。
默认工具范围为 execute，文件写入与命令默认需要审批；命令在受信任宿主上运行，
不提供代码沙箱。文件 revision、审批范围、进程清理和日志机制见[工具模块](docs/modules/tools.md)。

## 开发与学习

常规检查和真实服务测试入口见[验证与验收](docs/modules/verification.md)。
最近保留的完整离线结果仍有一项 RAG Recall 失败；v0.4 真实三次主线仅 2/3 通过。

后续实质性模块遵循[模块交付与学习工作流](docs/development/module-delivery-workflow.md)，
工程验收和学习出口分别判断。v0.5 可用 `/compact` 提前压缩、`/context-window` 查看占用，
通过自然语言检索历史、记住或停用内容；参见[上下文](docs/modules/context.md)与
[记忆](docs/modules/memory.md)。后台作业可用 `jdagent maintain --pending` 续做。
本轮实现、测试、独立审查及未完成验收见[实现记录](docs/reviews/REV-20261022-001-v0.5-implementation.md)。

Agent 开始工作按 [AGENTS.md](AGENTS.md) 进入 Map、当前状态和协作规则。
旧版设计及已完成计划见[归档索引](docs/archive/README.md)。
