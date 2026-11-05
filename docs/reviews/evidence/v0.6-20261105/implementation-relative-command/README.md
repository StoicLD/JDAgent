# 相对日志目录修复验证

状态：`Implemented`（验证记录），日期：2026-11-05。

以 `7026431` 为基准，修改原有控制器流程用例，使其按操作说明从相对实验目录启动。
修复前 [relative-before.xml.gz](relative-before.xml.gz)：**2 failed，2.85 秒**；
原生 B1 在实际验证子进程执行后，因相对 process.json 无法写入而失败。

CommandLogs 构造时将 root 转为绝对路径后，[relative-fixed.xml.gz](relative-fixed.xml.gz)：
**67 passed，65.33 秒**。运行入口如下，另指定任务内 basetemp 和 JUnit 输出路径：

```text
python -X utf8 -m pytest tests/test_v06_comparison_controller.py tests/test_v06_comparison_task.py tests/test_v04_commands.py tests/test_v06_tool_matrix.py tests/test_v06_recovery_matrix.py --tb=short -q
```

修复前只运行 `test_controller_runs_ordered_samples_and_enforces_frozen_ceilings` 的两个参数化用例。
所有模型传输均为 MockTransport，凭据为 dummy-key；本地真实子进程负责合成夹具验证。
全量 Ruff 格式／Lint、Pyright strict 通过；没有重跑或合并计算此前完整套件。
关联[真实失败与处置](../../../v0.6-live-comparison-20261105.md)。
