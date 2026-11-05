# v0.6 后台阶段恢复与租约接管

状态：`Implemented`（验证记录），日期：2026-11-05。基准 `ad55d77`。

[JUnit XML](maintenance-recovery.xml.gz)：12 passed，3.57 秒。
命令：`python -X utf8 -m pytest tests/test_v06_maintenance_recovery.py --tb=short -q`，另指定 basetemp 和 JUnit 文件。
Ruff 与全量 Pyright strict 通过。

四组合各测试提取后取消、合并后取消、合并后 lease 接管。阶段缓存实际写入 SQLite，之后关闭并重建
运行时和 Store；不依赖内存对象复用。前台、提取、合并各一条假 HTTP，12/4 的测试 Usage 逐条结算，
累计 36/12；重启不清零、不重复模型调用或记忆版本。旧 lease 无法发布，也不能把继任者状态改回待办。

租约过期使用显式 claim 时间推进，不等待真实 120 秒；阶段中断使用取消注入，不冒充独立进程强退。
真实进程强退另见[恢复矩阵](../implementation-recovery/README.md)。没有真实模型消费。
