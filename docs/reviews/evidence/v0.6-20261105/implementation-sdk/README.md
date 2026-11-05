# v0.6 SDK 结构化流错误收尾证据

状态：`Implemented`（测试记录），日期：2026-11-05。基准 `ceb07da`。

[完整离线 JUnit XML](sdk-full.xml.gz)：434 passed、1 failed、3 skipped；11 个 integration 未选择，60.52 秒。
唯一失败仍为已知 RAG Recall 0.8125 < 0.85。全量 Ruff format／check 与 Pyright strict 通过。
命令：`python -X utf8 -m pytest -m 'not integration' --tb=short -q`，另指定独立 basetemp 和 JUnit 文件。

`test_incomplete_json_mode_is_a_model_failure_without_sdk_exception` 的 empty、length、content_filter
三项在修复前实际抛出 SDK 异常，修复后输出项目 ModelFailed。协议、预算、内部调用定向检查共 55 项通过。
全部为 Fake／MockTransport，未执行真实模型调用。阶段状态见[实施记录](../../../../plans/v0.6-implementation.md)。
