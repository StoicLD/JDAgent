# v0.2 Windows 终端附录与人工矩阵

状态：`Approved`

批准日期：2026-08-20

## 固定行为

- v0.2 使用 Prompt Toolkit 行式 REPL，不使用全屏 TUI。Windows Terminal/ConPTY 是主验收环境，
  PowerShell legacy console 是降级检查环境。
- `Enter` 提交，`Alt+Enter` 换行；多行粘贴作为一个 Prompt。Prompt Toolkit 默认历史与
  `Ctrl+R` 搜索启用，历史按 workspace 存在本机数据目录。
- 空闲输入与审批输入由 Prompt Toolkit bottom toolbar 持续显示 Session、模型、写权限模式和
  `idle/waiting_approval` 状态；生成、工具执行与取消期间没有输入框，由 Rich Live footer 持续显示
  `generating/running_tool/cancelling`。两种渲染器在所有权边界先停止前一个 Live，再接管底栏，避免
  并发重绘和 Prompt 拼接；`starting/exiting` 通过同一语义状态事件呈现。
- 运行中第一次 `Ctrl+C` 由 asyncio 取消当前 Turn，Agent Loop 尽力记录取消事实，应用吞掉该次
  cancellation 并回到输入态；空闲 Prompt 的 `Ctrl+C` 返回 130。EOF 与 `/exit` 返回 0。
- `close()` 幂等；Rich Live 在完成、警告、错误、取消和退出时停止。非 TTY、重定向或无颜色
  环境由 Rich 自动降级；Headless 从不创建 Rich/Prompt Toolkit。
- 关闭窗口、terminate、强杀与断电不保证 Python finally 执行，只依赖逐事件 fsync 和恢复合同。

## 人工矩阵

| 环境/能力 | 必查项 | 通过标准 |
| --- | --- | --- |
| Windows Terminal + PowerShell | 多行、粘贴、历史、补全、Markdown | 输入不拆 Turn，输出不与 Prompt 拼接 |
| Windows Terminal + 中文 IME | 组合输入、光标、提交 | 不丢字、不重复提交 |
| legacy console | 输入、无颜色降级、窄窗口 | 可用且不遗留控制序列 |
| stdout 重定向 | text/json | 无 ANSI，stdout 合同稳定 |
| 运行中/空闲 Ctrl+C | 取消/退出 | 分别回到 idle 与返回 130 |
| EOF、`/exit`、关闭窗口 | 生命周期 | 正常入口恢复终端；强关后按恢复分类处理 |
