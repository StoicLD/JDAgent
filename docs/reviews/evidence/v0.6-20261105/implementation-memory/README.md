# v0.6 直接记忆意图与版本状态验证

状态：`Implemented`（测试记录），日期：2026-11-05。基准 `f594292`。

[JUnit XML](memory-matrix.xml.gz)：21 passed，5.34 秒。运行入口为
`tests/test_v06_memory_matrix.py`、`tests/test_v05_memory.py`、`tests/test_v05_memory_runtime.py`、
`tests/test_v05_review_regressions.py`，使用 `pytest --tb=short -q`，另指定 basetemp 和 JUnit 文件。
Ruff 与 Pyright strict 通过。

新增 8 项覆盖四组合的绑定意图和伪造正文：新增、更正、停用、停用后更正、启用、过期、历史版本、
workspace 隔离、无二次审批、伪造参数不保存且不重试。伪造参数沿用原生的保守 unknown 停止分类，
没有因此写入记忆。模型全部为实际 Adapter 加 MockTransport，不调用真实服务。
后台阶段恢复和 lease 的现有组件用例仍不等同于完整组合证据，见[实施记录](../../../../plans/v0.6-implementation.md)。
