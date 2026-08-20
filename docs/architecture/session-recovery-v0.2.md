# v0.2 Session 异常恢复表示

状态：`Approved`

批准日期：2026-08-20

## 物理恢复

- 普通 `JsonlSession.read` 继续严格 fail closed，不忽略、不截断坏记录。
- 只有“所有完整行均通过 JSON、Schema、session ID 与连续 sequence 校验，且最后一条物理记录
  无换行”可以修复。
- 修复先把原字节写入带 UTC 时间与 SHA-256 摘要的备份并 `fsync`，再把完整前缀写入临时文件、
  `fsync` 后原子替换。中间损坏、空前缀、Schema 不支持和 sequence 冲突不修复。

## 逻辑恢复

| 最后完整 Turn 后的事实 | 分类 | 动作 |
| --- | --- | --- |
| 没有开放 Turn | `clean` | 直接恢复 |
| 未开始工具，或只有已开始但未完成的只读工具 | `interrupted_safe` | 追加 `turn_failed(cancelled, process_interrupted)` 后恢复 |
| 写工具已开始但没有完成结果，或风险未知 | `uncertain_side_effect` | 禁止续跑原 Session |

副作用不确定时，交互式 `/resume` 是用户的显式恢复动作：保留原 Session，建立一个新 Session，
追加 `session_started` 和 `recovery_snapshot`。Snapshot 固定记录 `parent_session_id`、最后安全终止
事件的 sequence，以及由该安全前缀投影出的模型消息。ContextBuilder 只读取 Snapshot 的消息；它
不复制 event ID，不继承未决工具，不回滚 workspace，也不是通用 branch。Headless 恢复遇到该
状态直接返回 Session 错误并引导用户进入交互模式。

