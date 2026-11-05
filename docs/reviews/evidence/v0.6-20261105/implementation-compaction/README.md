# v0.5 压缩对象兼容验证

状态：`Implemented`（验证记录），日期：2026-11-05。基准 `5b446a1`。

[JUnit XML](legacy-compaction.xml.gz)：12 passed，3.44 秒。
命令：`python -X utf8 -m pytest tests/test_v06_legacy_compaction.py --tb=short -q`，另指定 basetemp 与 JUnit 文件。
Ruff 与全量 Pyright strict 通过。

固定 v0.5 源码实际产生 11 个会话／对象，异目录重建逐字节相同。四组合分别测试完整、缺失和损坏
checkpoint，再用 native/native 继续同一会话；检查保护约束、摘要／原文选择、历史前缀、请求重建、
无重复压缩和工具调用。所有模型为本地 Fake／MockTransport。样本及来源见
[数据说明](../../../../../testdata/v0.5-compacted/README.md)，不代表真实长任务或全部旧数据规模验收。
