# 验证与验收

状态：`Implemented`（证据导航与状态说明）

这里区分实现存在、测试通过、真实服务验证和学习验收。基准见[Map](../map.md)。
下表是保留的历史运行结果，不是 2026-10-20 重跑完整产品测试的结论。

## 最近证据

| 范围与日期 | 结果 | 能证明的范围 |
| --- | --- | --- |
| v0.1，2026-08-20 完成 | 工程、DeepSeek 与 M10b 学习出口通过 | 首版 Runtime；[审计](../reviews/v0.1-completion-audit.md) |
| v0.2，2026-08-20 工程验证 | 113 passed、3 skipped；Live、安装与终端主路径通过 | 当时 CLI 实现；[实现审查](../reviews/v0.2-implementation-review.md) |
| v0.3，2026-09-05 修复后 | 197 passed、1 failed、5 skipped | 自测与限范围回归；工程复核 fail；[审查](../reviews/REV-20260905-001-v0.3-implementation-audit.md) |
| v0.4，2026-09-23 完整离线 | 236 passed、1 failed、8 skipped；Ruff、Pyright、安装通过 | 离线链及 Windows 文件／进程机制；[实施记录](../plans/v0.4-implementation.md) |
| v0.4，2026-09-23 真实 DeepSeek | 1 failed、2 passed，基准 `7e18a1a` | 三次固定工具任务；[完整 Live 记录](../reviews/v0.4-live-verification.md) |

v0.4 实现的 Standards／Spec 独立评审在修复后定向复核通过，见[处置记录](../reviews/v0.4-implementation-review.md)。
该报告前部“Live 未执行”是当时事实，末尾已追加后续结果，最新结论以三次 Live 记录为准。

## 仍开放的工程门禁

v0.3 唯一离线失败是 `test_offline_child_and_ptk_eval_meet_gates`：
正确按来源身份计分后词项 Recall@10 为 `0.8125`，低于 `0.85`。
不能用旧的忽略来源身份的分数证明验收通过。真实 Milvus Live 未完成，
来源恢复／删除／引用核验／真实配置等缺口见[知识模块](knowledge.md)。

v0.4 三次真实任务都修复了夹具错误并获得 exit 0，但首次遗漏必需的 `search_text`，
所以没有达到“三次全部覆盖主线”的门槛。逐次原始 JSON、事件与用量保存在
[证据 manifest](../reviews/evidence/v0.4-live-20260923/manifest.json)。
这些结果验证固定工具链；夹具带有错误说明，不能当作盲测诊断能力的证据。

Q23 按实际依赖处理跨版本阻塞：相关权限、Session、工具与基础回归问题须处理；
无关 RAG 质量缺口单独跟踪。既有失败不因此变成通过，新结果也不能覆盖失败尝试。

## 测试入口

| 模块 | 优先测试 |
| --- | --- |
| Runtime／Context／模型 | `test_agent_loop.py`、`test_runtime_controls.py`、`test_context_builder.py`、`test_deepseek_adapter.py` |
| 工具／权限／进程 | `test_tool_runtime.py`、`test_approval.py`、`test_v04_contracts.py`、`test_v04_files.py`、`test_v04_commands.py`、`test_v04_e2e.py` |
| Session／CLI | `test_jsonl_session.py`、`test_session_recovery.py`、`test_forced_exit_recovery.py`、`test_interactive_cli.py`、`test_configuration.py` |
| RAG | `test_ingestion.py`、`test_source_revision_regressions.py`、`test_retrieval.py`、`test_citation.py`、`test_v03_eval.py` |

上述文件都位于 [tests](../../tests)。工具 Eval 使用[v0.4 固定夹具](../../testdata/v0.4-tools/DATASET.md)，
检索使用[v0.3 Gold](../../testdata/v0.3-gold/DATASET.md)。

在产品根目录执行常规检查：

```powershell
uv run ruff format --check .
uv run ruff check .
uv run pyright
uv run pytest -q
```

真实 DeepSeek 测试显式设置 `JDAGENT_RUN_DEEPSEEK_INTEGRATION=1`，
Milvus 测试另用 `JDAGENT_RUN_RAG_LIVE=1`；入口位于 [integration](../../tests/integration)。
需要真实服务和凭证的检查与离线检查分别记录；未启用而 skip 不算 Live 通过。

## 学习出口

- [v0.1](../learning/v0.1-learning-checklist.md)：2026-08-20 完成。
- [v0.2](../learning/v0.2-learning-checklist.md)：2026-08-24 所有者按范围关闭，原练习供复习。
- [v0.3](../learning/v0.3-rag-learning-checklist.md)：2026-09-16 所有者裁定关闭，工程仍未通过。
- [v0.4](../learning/v0.4-tools-learning-checklist.md)：学习及人工语义验收待本人完成。

学习结论不能替代工程结论；AI 的报告核对也不替代学习者的人工出口。
