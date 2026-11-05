# v0.5 会话副本兼容验证

状态：`Implemented`（测试记录），日期：2026-11-05。基准 `7e727b3`。

[JUnit XML](legacy-sessions.xml.gz)：16 passed，5.15 秒。命令：
`python -X utf8 -m pytest tests/test_v06_legacy_sessions.py --tb=short -q`，另指定独立 basetemp 和 JUnit 文件。
Ruff 与 Pyright strict 通过；未执行 Live。

四个由固定 v0.5 源码生成的最小会话样本分别进入四种新旧组合，然后使用原生后端打开同一数据。
检查原始字节、保护约束、安全中断、两种 unknown、恢复快照及不重放写入。样本和来源指纹见
[数据说明](../../../../../testdata/v0.5-sessions/README.md)。带压缩对象和知识依赖的旧会话覆盖另行跟踪，
本记录不把最小合同样本扩大为全部历史数据验收。
