# 实现自审与收尾记录校验

状态：`Implemented`（验证记录），日期：2026-11-05。
自审输入：`40946efc47401e3ff06c80f076556bb7efb7bd09`。

- [完整离线 JUnit](prelive-full.xml.gz)：545 passed、1 个既有 RAG 失败、3 skipped、11 deselected，85.25 秒。
- [F01 修复后控制器 JUnit](comparison-sealed.xml.gz)：15 passed，9.87 秒。
- [自审与处置](../../../REV-20261105-002-v0.6-implementation-self-review.md)。

完整命令：`python -X utf8 -m pytest -m 'not integration' --tb=short -q`，另指定 basetemp 和 JUnit 文件。
全量 Ruff 格式／Lint 和 Pyright strict 通过；300 个文件格式符合规范。唯一失败仍为
`test_offline_child_and_ptk_eval_meet_gates` 的 Recall `0.8125 < 0.85`。

修复前命令：`python -X utf8 -m pytest tests/test_v06_comparison_controller.py::test_completed_measurement_edit_is_rejected_before_next_attempt --tb=short -q`。
该用例在未修复控制器上实际报告 `1 failed in 2.17s`，输出摘录：

```text
await comparison.execute(experiment, "B2", api_key="dummy-key", authorized=True)
await run_fixed_task(...)
AssertionError: Edited measurement must be rejected before task execution
```

失败表明修改 B1 完成计时后，B2 仍进入任务入口；该入口在测试中被拒绝，未发送 B2 请求。
修复后重新运行整个控制器测试文件，新增内容指纹、首次目标前的改写阻断及非法数值验证全部通过。
后者是工作树修复的定向结果，没有合并进此前完整运行计数。所有模型均为 Fake／MockTransport。
