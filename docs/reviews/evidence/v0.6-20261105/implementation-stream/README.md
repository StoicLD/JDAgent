# v0.6 模型流关闭修复证据

状态：`Implemented`（测试记录），日期：2026-11-05。基准 `5a39c29`；本目录所在提交保存修复。

[完整离线 JUnit XML](stream-full.xml.gz)：362 passed、1 failed、3 skipped；11 个 integration 未选择。
失败仍为既有 RAG Recall 0.8125 < 0.85，skip 为 opt-in 的存储规模用例。运行命令为
`python -X utf8 -m pytest -m 'not integration'`，另指定独立 basetemp 和 JUnit 文件。

`test_priority_slot_waits_for_underlying_stream_close_before_reuse` 在修复前实际失败：消费者关闭完成时，
底层仍阻塞于清理事件。修复后该用例与图差分、压缩、取消共 49 项定向检查通过；全量也包含此回归。
模型全部使用 Fake／MockTransport。没有执行 Live。背景和后续范围见[实施记录](../../../../plans/v0.6-implementation.md)。
