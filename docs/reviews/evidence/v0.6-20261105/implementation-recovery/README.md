# v0.6 工具效果、崩溃恢复与回退证据

状态：`Implemented`（测试记录），日期：2026-11-05。基准 `7059c34`。

[JUnit XML](recovery-matrix.xml.gz)：36 passed，41.57 秒。
命令：`python -X utf8 -m pytest tests/test_v06_recovery_matrix.py tests/test_v06_tool_matrix.py --tb=short -q`，
另指定独立 basetemp 和 JUnit 文件。Ruff 与 Pyright strict 通过。

四组合各执行五项工具／取消场景和四项强制退出／恢复／原生回退场景。模型 HTTP 为 MockTransport；
文件编辑、Python 校验命令、Session JSONL、文件锁、独立进程退出是真实操作，均限制在测试临时目录。
写入开始但没有结果的情况保持 unknown，不推断为未执行；恢复快照不改父历史。结果落盘后、工具
节点返回前的退出不会再次执行写入。完整版本和历史 v0.5 会话副本兼容性仍按[实施记录](../../../../plans/v0.6-implementation.md)跟踪。
