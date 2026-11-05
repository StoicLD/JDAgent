# v0.6 共用实验预算验证证据

状态：`Implemented`（测试记录），日期：2026-11-05。基准 `326a064`；本目录所在提交保存实现。

[完整离线 JUnit XML](budget-full.xml.gz)：395 passed、1 failed、3 skipped；11 个 integration 未选择。
失败仍为既有 RAG Recall 0.8125 < 0.85，skip 为 opt-in 的存储规模用例。
命令：`python -X utf8 -m pytest -m 'not integration' --tb=short`，另指定独立 basetemp 和 JUnit 文件。
全量 Ruff format、Ruff check 和 Pyright strict 通过。

新增 33 项用例包含：两个真实适配器的假 HTTP 发送前阻断、所有调用种类、输入／输出上限、缺失与
重复 Usage、断流、503 不重试、取消关闭、重启保留、重复 ID／结算、并发争抢、损坏账本和超额实际
用量；四种组合从运行时入口执行压缩、普通任务、引用修复、后台记忆维护，核对共用账本及客户端关闭。

组合测试曾发现引用修复发送元数据没有 `call_kind=citation_repair`，已与归档分类统一。LangChain
JSON mode 初次测试还暴露了夹具缺少 SSE envelope 和 role，修正夹具后通过，没有放宽适配器校验。
模型全部为 Fake／MockTransport，没有执行真实模型调用。后续范围见[实施记录](../../../../plans/v0.6-implementation.md)。
