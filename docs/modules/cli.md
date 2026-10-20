# CLI 与配置

状态：`Implemented`（实现说明）

CLI 提供持久交互与单次 Headless 两种入口，共用 TurnCoordinator 和 Runtime。
实现基准见[Map](../map.md)，最短启动步骤见[README](../../README.md)。

## 两种入口

无 Prompt 时进入 Prompt Toolkit 行式 REPL，Rich 展示流式 Markdown、状态与审批。
应用层管理 idle、运行、等待审批、执行工具、取消和退出等短暂状态，终端 Adapter 处理输入输出。
运行中 Ctrl+C 取消本轮并回到 idle；空闲 Ctrl+C 退出 130；`/exit` 或 EOF 正常退出并保留 Session。

提供 Prompt 时运行一轮并返回 text 或 JSON。Headless 不阻塞等待交互审批。
JSON 输出当前为 schema v2，包含回答、停止原因、用量、知识状态、引用和模型补充；
Session 事件 schema v3 是不同合同。

退出码：成功 `0`，内部错误 `1`，用法／配置错误 `2`，会话错误 `3`，
运行错误 `4`，取消 `130`。退出 0 代表 Runtime 正常结束，不代表任务的独立验收通过。

## 当前命令

| 命令 | 用途 |
| --- | --- |
| `/help` | 查看命令帮助 |
| `/status` | 查看当前状态 |
| `/new` | 新建 Session |
| `/sessions` | 发现当前 workspace 的 Session |
| `/resume` | 选择并恢复 Session |
| `/rename` | 命名 Session |
| `/permissions` | 查看与撤销 Session 允许规则 |
| `/trace` | 查看结构化执行轨迹 |
| `/knowledge` | 查看 workspace 知识绑定和本轮知识状态 |
| `/logs` | 查看／清理当前 Session 的命令日志 |
| `/exit` | 退出交互 |

当前枚举为 11 条；早期 v0.2 的“九条命令”是历史范围。
知识管理还提供 `jdagent knowledge ...` 独立命令树，负责连接、知识库、来源和 Binding 等操作；
它的真实 RAG 配置限制见[知识模块](knowledge.md)。

## 配置与数据

普通选项按 CLI > 项目 > 用户 > 默认值解析；权限配置存在额外的 DENY 上限，
不能用更高优先级配置放宽用户／项目拒绝。
默认 Provider 为 DeepSeek，工具范围 execute，写与命令权限 ask。
模型／工具超时和上下文窗口由配置提供，完整参数以 `jdagent --help` 为准。

DeepSeek Key 优先读取 `DEEPSEEK_API_KEY`，其次使用用户配置 `api_key_file`，
最后回退到开发期 `../tmp/keys/deepseek-api-key.txt`。
开发文件位于产品仓库外；此处只描述既有读取机制，不把外层目录加入项目文档启动链。

`DataPaths` 根据规范 workspace 路径生成身份，在本机用户数据目录建立 workspace 分区，
保存 Session、Catalog、输入历史和 UI 状态。项目配置使用 workspace 中的 `.jdagent/config.toml`；
知识 Catalog／Store 等用户级数据与 workspace Binding 分开管理。
默认 Session 子目录和迁移边界见[会话模块](sessions.md)。

## 代码与验证入口

- [CLI 入口](../../src/jdagent/cli.py)、[交互应用](../../src/jdagent/application/interactive.py)、
  [Headless 合同](../../src/jdagent/application/headless.py)
- [配置解析](../../src/jdagent/configuration.py)、[数据目录](../../src/jdagent/data_paths.py)、
  [宿主组装](../../src/jdagent/host.py)、[终端 Adapter](../../src/jdagent/adapters/terminal.py)
- [交互测试](../../tests/test_interactive_cli.py)、[入口测试](../../tests/test_cli_entrypoint.py)、
  [子进程测试](../../tests/test_cli_subprocess.py)、[配置测试](../../tests/test_configuration.py)
- [ADR-0004](../decisions/ADR-0004-cli-application-layer.md)、
  [v0.2 历史 CLI 设计](../archive/v0.2/cli-application.md)、
  [Windows 人工矩阵](../archive/v0.2/windows-terminal.md)

中文 IME、legacy console 和真实窗口强关等人工矩阵不能由普通自动化测试全部替代；
历史验证范围见[验证模块](verification.md)。
