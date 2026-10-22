# 会话与事件

状态：`Implemented`（实现说明）

Session 保存对话和执行事实，模型上下文、Trace、Session Catalog 都由事实投影生成。
当前实现基准见[Map](../map.md)。

## 事实所有权

`SessionPort` 提供追加和读取事件；`EventJournal` 分配严格递增序号，
先持久化事件，再更新进程内序列并通知观察者。持久化失败时不把事件当作已经保存。
高频文本增量另供实时展示，最终 assistant 消息才成为可恢复事实。

事件记录用户输入、完整模型消息、工具请求、权限决定、规则授予／撤销、工具开始与结束、
用量、检索元数据、窗口裁剪以及 Turn 完成／失败。Trace 消费同一事件流，
只导出安全结构信息；它不替代 Session，也不保存另一份业务结论。

## 持久化与版本

正常组合根使用 append-only JSONL；测试可用 InMemory Adapter。
JSONL 每行一个事件，追加完成包含 flush/fsync；读取校验 schema、Session ID 和严格序号。
当前事件 `schema_version=4`，默认数据分区为 `sessions-v5`。显式读取旧格式会被拒绝，
不自动导入、迁移或删除；仓库里的 legacy Adapter 不等于正常启动仍会调用它。

大于 16 KiB 的事件正文以不可变对象保存，日志只记录 hash 和字节数；读取时校验完整性。
同一 Session 由 OS 独占锁保护，持锁追加缓存已校验尾部，避免每次重读整个文件。

Headless 输出使用独立的 JSON v2，包版本为 0.5.0，三者不共用版本号。
数据位置与 workspace 分区方式见[CLI 与配置](cli.md)。

## 恢复

恢复分为物理文件检查和逻辑 Turn 检查。

| 情况 | 当前处理 |
| --- | --- |
| JSONL 完整、Turn 已结束 | 按正常 Session 恢复 |
| 仅最后一条记录残缺且完整前缀有效 | 显式恢复先备份，再有限修复尾部；继续逻辑检查 |
| 中间记录损坏、未知 schema 或序号冲突 | 拒绝普通恢复，保留现场 |
| 中断但无未知写入／命令副作用 | 补齐待返回的工具结果和中断事实，允许安全继续 |
| 写入／命令已开始但结果缺失，或结果明确未知 | 隔离到新的恢复 Session，通过快照保留必要上下文 |

恢复快照只引用父安全前缀、hash、checkpoint 与未知操作，不复制完整消息，也不伪装成用户输入。
未知副作用不能因已经存在 `turn_failed` 而忽略。新恢复 Session 不继承许可，
可只读访问来源 Session 的命令输出；缺少最终元数据的日志显示 incomplete。
恢复不会重新执行上次未确认的写入或命令。
父会话独占从物理检查持续到恢复提交；取消取锁、修复或子快照写入时，等待本地事务结束后
再传播取消，不把仍在线程中执行的写入误当成已经停止。已有前台所有权保持不变。

模型一次请求多工具而批次中止时，未开始调用补 `not_executed`；
已开始且无法确认副作用的调用保留 unknown。这样后续上下文保持工具调用与结果配对，
也不会把“不知道是否执行”伪装成“未执行”。

## Session 发现与权限

Catalog 管理会话名称、短 ID 和发现列表，其索引可重建。
Session 级规则的授予与撤销都是事件；`/exit` 后恢复同一正常 Session 仍有效。
工作区之间、不同 Session 之间不共享这些允许规则；具体权限边界见[工具模块](tools.md)。

## 代码与验证入口

- [事件类型](../../src/jdagent/domain/events.py)、[EventJournal 与配对补全](../../src/jdagent/eventing.py)
- [JSONL Adapter](../../src/jdagent/adapters/jsonl_session.py)、[物理恢复](../../src/jdagent/adapters/jsonl_recovery.py)
- [逻辑恢复](../../src/jdagent/application/session_recovery.py)、[Catalog](../../src/jdagent/application/session_catalog.py)、
  [Trace](../../src/jdagent/observability.py)
- [JSONL 测试](../../tests/test_jsonl_session.py)、[恢复测试](../../tests/test_session_recovery.py)、
  [强制退出测试](../../tests/test_forced_exit_recovery.py)、[v0.4 配对与未知状态测试](../../tests/test_v04_contracts.py)
- 历史来源：[v0.2 生命周期](../archive/v0.2/session-lifecycle.md)、
  [恢复设计](../archive/v0.2/session-recovery.md)；v0.4 增量见[实现记录](../plans/v0.4-implementation.md)。
