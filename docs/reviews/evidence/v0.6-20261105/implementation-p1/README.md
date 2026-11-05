# v0.6 首个模型适配切片的离线证据

状态：`Implemented`（测试记录），日期：2026-11-05。

三份 gzip 文件保存本地运行的原始 JUnit XML。实现范围及限制见
[实施记录](../../../../plans/v0.6-implementation.md)。固定源码由包含本目录的 Git 提交保存。

| 文件 | 实际范围与结果 |
| --- | --- |
| baseline.xml.gz | 新依赖最初解析后，产品代码尚未修改：271 passed、3 failed、3 skipped；两项旧版本断言与一项 RAG Recall 失败 |
| p1-targeted.xml.gz | 新适配器、组合根、配置、CLI、端口与包版本：55 passed |
| p1-full.xml.gz | 完整离线集合：299 passed、1 failed、3 skipped；唯一失败为既有 RAG Recall 0.8125 < 0.85 |

全量运行命令为 `.venv/Scripts/python.exe -X utf8 -m pytest -m 'not integration'`，另指定隔离 basetemp
及 JUnit 路径；11 个 integration 用例未选择。三项 skip 为显式 opt-in 的存储规模用例。
全部模型请求使用 Fake 或假 HTTP，未调用真实模型。结果不证明 StateGraph、完整组合矩阵或 Live 通过。
