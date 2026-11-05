# v0.6 StateGraph 切片的离线证据

状态：`Implemented`（测试记录），日期：2026-11-05。基准为 `c789e0e`，本目录所在提交保存对应实现。

| 文件 | 实际范围与结果 |
| --- | --- |
| p2-targeted.xml.gz | 主循环、运行控制、图差分、四组合接线、压缩、CLI 和配置：109 passed |
| p2-full.xml.gz | 完整离线集合：361 passed、1 failed、3 skipped；11 个 integration 用例未选择 |

唯一全量失败为既有 RAG Recall `0.8125 < 0.85`；三项 skip 为显式 opt-in 的存储规模用例。
使用 `python -X utf8 -m pytest -m 'not integration'` 并指定隔离 basetemp／JUnit 输出运行全量。
全部模型为 Fake 或真实适配器接 MockTransport，没有 Live 模型请求。

关键证据包括：新旧请求／事件／计数／完整返回文本差分，工具顺序、历史与本轮 ID、规则跳过后的
计数和下一 Turn 拒绝、修复协议差异、模型／工具满额度、图限额收尾、并发 Session 隔离、模型取消
关闭、工具取消清理、持久化错误和终态后观察者异常。三次压缩在两个引擎执行；四组合通过普通问答、
逐轮配置与归档、后端重启检查。四组合全部工具、恢复及故障矩阵仍待补齐。

全量 Ruff 格式／Lint 与 Pyright strict 通过。完整完成条件、未验证项和后续提交入口见
[实施记录](../../../../plans/v0.6-implementation.md)。
