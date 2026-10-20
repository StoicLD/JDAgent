# 工具与权限

状态：`Implemented`（实现说明）

工具模块负责把模型请求变成受控执行，并准确返回完成情况与副作用。
当前八工具和离线链已有实现，完整工程验收仍开放；基准见[Map](../map.md)，结论见[验证](verification.md)。

## 注册与调用

`ToolRegistry` 保存名称、描述、输入／结果 Schema、风险、preflight 与 Handler，拒绝重名。
`ToolRuntime.execute()` 依次检查调用身份、工具存在性与可用范围、参数 Schema、
preflight、权限和审批，再记录执行开始、调用 Handler、规范化并校验结果。
Loop 保存最终 `tool_execution_completed`，并决定继续还是停止。

| 工具 | 能力 | 最低范围 |
| --- | --- | --- |
| `calculator` | 有限算术计算 | analyze |
| `find_files` | 有界文件发现，遵守忽略规则 | analyze |
| `read_file` | 读取 UTF-8 文本并返回完整文件 revision | analyze |
| `search_text` | 有界文本搜索 | analyze |
| `read_command_output` | 按日志 ID 和字节范围读取输出 | analyze |
| `create_file` | 独占创建，不覆盖已有文件 | edit |
| `edit_file` | 校验 revision 后按精确文本批量编辑 | edit |
| `run_command` | 执行明确程序与 argv，管理进程树和输出 | execute |

`--tool-scope` 默认 `execute`；`edit` 包含 analyze，execute 包含 edit。
工具定义投影和实际执行使用同一范围，隐藏工具不能靠伪造调用获得执行。
旧三工具工厂保留给既有合同回归，正常组合根使用上表八工具。

## 权限和审批

Policy 返回 `ALLOW | ASK | DENY`，ApprovalPort 仅在 ASK 时收集决定。
文件写入和命令默认 ASK；用户／项目 DENY 是上限，Session 规则和 CLI 覆盖不能放宽。
`write_permission=deny` 也禁止命令执行，避免通过命令绕过写限制。

审批展示准备后的完整参数，包括编辑旧／新文本与命令。持久审批事件保存摘要及展示哈希，
Trace 不输出正文。同轮相同准备后调用被拒绝后不重复询问，即使模型换了调用 ID。
Session 规则持久、可撤销；恢复同一正常 Session 时有效，新 Session 不继承。

命令 Session 规则匹配程序、cwd、环境指纹和非空参数前缀；用户确认后允许前缀后的任意尾参。
这项范围应以完整审批展示理解，不能把它解释成固定整条命令授权。

## 文件操作

文件工具限制在规范 workspace 下，支持普通本地文件，拒绝越界、设备、ADS、链接、
reparse point 和多硬链接目标。发现与搜索共享忽略规则，并有扫描数量／时间上限。
读取与编辑的文件上限为 2 MiB；受支持文本为 UTF-8，可保留 BOM。

revision 绑定规范路径和原始完整字节。编辑在 Windows 独占句柄下复核 revision、计算整批匹配，
再原位写入、truncate、flush/fsync；冲突或匹配失败不写入。换行匹配兼容 CRLF/LF，
保留未修改字节。原位提交不承诺崩溃原子性；中断写入可能部分生效，必须记录未知副作用。

## 命令与日志

命令使用显式程序与 argv，不隐式调用 Shell，拒绝直接启动 bat/cmd。
受信任 Python bootstrap 使用 `-I -S`，先进入 kill-on-close Windows Job，再启动用户程序。
命令仅继承宿主环境白名单，不传 Provider 密钥或 Python 启动钩子。
这些机制用于受信任宿主上的协作执行，不构成不可信代码沙箱。

命令生命周期在工作线程运行；取消通知线程，Runtime 等待进程树和输出清理，包括重复取消。
握手区分“程序尚未启动”与“已执行”；无法确认启动或清理时按未知处理并停止。

stdout/stderr 分开留存，单次合计 10 MiB、workspace 合计 100 MiB、预览合计 16 KiB。
达限仍排空管道并标记丢失；容量不足拒绝新命令。`/logs clear` 只清当前 Session 输出并保留标记。

## 结果语义

`ToolOutcome` 分开表达 `completed`、`effect`、`observed_failure`、`cancelled`、
`error_code`、`requires_stop`。命令非零退出码可以是“执行已完成、验证观察到失败”；
启动失败不能声称测试通过或失败。未知副作用要求停止，并交给[会话恢复](sessions.md)处理。
七个文件／命令工具具有成功结果 Schema；calculator 保留字符串结果路径。

## 代码与验证入口

- [注册与 Runtime](../../src/jdagent/tools/runtime.py)、[工具类型](../../src/jdagent/domain/tools.py)、
  [权限](../../src/jdagent/tools/permissions.py)、[审批](../../src/jdagent/application/approval.py)
- [文件工具](../../src/jdagent/tools/files.py)、[Windows 文件句柄](../../src/jdagent/adapters/windows_files.py)
- [命令工具](../../src/jdagent/tools/commands.py)、[Windows 进程](../../src/jdagent/adapters/windows_process.py)、
  [日志](../../src/jdagent/tools/command_output.py)
- [文件测试](../../tests/test_v04_files.py)、[命令测试](../../tests/test_v04_commands.py)、
  [整链测试](../../tests/test_v04_e2e.py)、[Runtime 测试](../../tests/test_tool_runtime.py)
- [实施合同与授权](../plans/v0.4-implementation.md)、[历史设计](../archive/v0.4/tool-execution-proposal.md)
