# 统一比较任务离线演练

状态：`Implemented`（验证记录），日期：2026-11-05。基准 `deb36bf`。

[JUnit XML](comparison-task.xml.gz)：9 passed，6.71 秒。
命令：`python -X utf8 -m pytest tests/test_v06_comparison_task.py --tb=short -q`，另指定 basetemp 与 JUnit 文件。
Ruff 与全量 Pyright strict 通过。

任务执行层见[comparison_task](../../../../../tests/v06_comparison_task.py)。单项成功演练顺序运行
native/native 三次及 langgraph/langchain 三次，共用一个 SQLite 预算账本，每次 9 个请求，合计 54 个。
其余 8 项覆盖两组合的预算阻断、503 未知预留、取消留档，以及修复正确但遗漏必要搜索的失败判断。
所有传输由 MockTransport 提供，只运行临时合成项目中的 Python 校验命令；dummy key 不写入报告。

`setup_ms` 是进程内任务准备与组合根构造耗时，不能当作新进程启动时间。首文本从提交用户任务计时，
压缩输出不进入该观察者。成本记录来自全部外部尝试，不能只用 TurnResult 的普通任务调用计数。
本演练不证明真实模型、完整 Live 控制入口或默认切换通过。
