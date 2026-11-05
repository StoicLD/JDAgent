# 固定实验控制入口验证

状态：`Implemented`（验证记录），日期：2026-11-05。基准 `9eb76d8`。

[JUnit XML](comparison-all.xml.gz)：统一任务 9 项＋控制器 14 项，**23 passed，13.87 秒**。
命令：`python -X utf8 -m pytest tests/test_v06_comparison_task.py tests/test_v06_comparison_controller.py --tb=short -q`，
另指定 basetemp 和 JUnit 文件。Ruff 与全量 Pyright strict 通过；两个 CLI help 入口已执行。

控制器测试分别运行成功比较和上限故意为零的失败比较；固定脚本在两组各执行三次，全部请求经
MockTransport。检查顺序、不可重跑、判据冻结／修改阻断、计时口径、计划／环境／夹具变化、预算
丢失或损坏、结果改写、缺少 opt-in、停止后中断记录以及获取锁期间连续取消。控制器停止后可重新
取得 OS 锁，未知费用不被清零。

测试为隔离准备过程而固定仓库／环境快照；真实 prepare 的本机清单另行核查，不能以这些 Mock
证明真实环境指纹稳定。所有调用使用 synthetic key，未访问真实模型。操作和限制见
[说明](../../../../development/v0.6-comparison-runbook.md)。
