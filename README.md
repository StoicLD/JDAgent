# JDAgent

状态：`Implemented`（使用说明），更新：2026-10-23，当前版本：`0.5.0`。

JDAgent 是用于实践 Agent 核心机制的 Python 命令行项目。目前支持 DeepSeek 对话、持久会话、
项目文件读写、命令执行、上下文压缩、历史检索和长期记忆。知识库已有管理与摄取入口，
但真实 RAG 配置链仍有缺口。当前主要运行和验证环境为 Windows。

**完整操作步骤见[安装与使用教程](docs/guides/usage.md)。**

## 安装与离线启动

在 PowerShell 中进入产品仓库，执行：

```powershell
Set-Location 'E:\Projects\Codex_Projects_Overall\JDAgent\JDAgentProject'
uv sync --locked
uv run jdagent --version
uv run jdagent --provider fake --output json "离线测试"
```

需要 Python 3.11+ 和 uv，仓库指定 Python 3.11.15。尚未安装环境或首次克隆时，按
[安装教程](docs/guides/usage.md#安装与更新)操作。预期版本为 `jdagent 0.5.0`。
Fake 重放固定脚本，用于检查运行链路，不能代替真实模型分析文件或验证记忆、压缩质量。

## 使用 DeepSeek

在当前 PowerShell 输入 Key，再启动交互模式：

```powershell
$env:DEEPSEEK_API_KEY = [System.Net.NetworkCredential]::new('', (Read-Host 'DeepSeek API Key' -AsSecureString)).Password
uv run jdagent --provider deepseek --tool-scope analyze
```

默认模型标识为 `deepseek-v4-flash`。直接输入问题，`/help` 查看命令，`/exit` 退出。
`analyze` 适合先阅读代码；真实问答、压缩与后台记忆整理会使用模型服务。

## 常用操作

以下命令在产品仓库根目录的 PowerShell 执行，工作区替换为已有项目目录：

| 目的 | 命令 |
| --- | --- |
| 阅读与搜索项目 | `uv run jdagent --workspace 'E:\Projects\Example' --tool-scope analyze` |
| 允许创建、编辑文件 | `uv run jdagent --workspace 'E:\Projects\Example' --tool-scope edit` |
| 允许编辑并执行命令 | `uv run jdagent --workspace 'E:\Projects\Example' --tool-scope execute --command-permission ask` |
| 单次问答后退出 | `uv run jdagent --tool-scope analyze "解释 ModelPort"` |
| 单次 JSON 输出 | `uv run jdagent --output json --show-trace "解释 ModelPort"` |
| 续做后台记忆作业 | `uv run jdagent maintain --pending --workspace 'E:\Projects\Example'` |

不传 `--workspace` 时使用当前目录。默认范围为 execute，文件写入与命令默认需要审批；
单次运行不会等待交互审批。命令在本机运行，不提供不可信代码沙箱。

下面输入到 **JDAgent 交互提示符**，不是 PowerShell：

```text
/status
/rename 上下文实验
/sessions
/context-window
/compact
/trace
/exit
```

`/resume <名称或ID>` 恢复会话，`/new` 开始新会话。`/compact` 提前压缩，原始历史仍保留。
自然语言 `记住：项目使用 pytest` 保存项目记忆；查询、更正和停用示例见
[历史与记忆操作](docs/guides/usage.md#历史检索与长期记忆)。没有 `/memory` 或 `/context` 命令。

## 文档与当前限制

- [安装与使用教程](docs/guides/usage.md)：安装、会话、审批、上下文、记忆、配置、数据和排错。
- [知识库操作教程](docs/guides/knowledge.md)：管理命令、文件摄取及当前 RAG 限制。
- [文档 Map](docs/map.md)：按模块理解实现；[当前状态](docs/context/current.md)记录剩余工作。
- [验证与验收](docs/modules/verification.md)：测试入口和原始证据。v0.5 修复后的三次固定
  DeepSeek 验收通过，完整故障矩阵、语义 Eval 等尚未完成，既有 RAG Recall 失败仍保留。

v0.5 使用新 Session 格式，不兼容旧会话，也不会自动清理旧目录。
Agent 开发入口为 [AGENTS.md](AGENTS.md)；工程与学习验收分别遵循
[模块交付与学习工作流](docs/development/module-delivery-workflow.md)。
