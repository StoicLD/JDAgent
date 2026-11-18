# 架构决策索引

状态：`Implemented`（导航）。更新：2026-11-11。

ADR 保存当时的理由、选择和批准状态；它不是当前实现的替代品。
当前代码是产品行为的唯一事实来源，[架构总览](../architecture/overview.md)说明实际依赖。
已有 ADR 的原始状态保留；归档旧方案不代表批准未决 ADR 或补齐未实现能力。

| 决策 | 当前阅读方式 |
| --- | --- |
| [0001 Python Core](ADR-0001-python-core.md) | Python 语言选择；当前依赖见 pyproject.toml |
| [0002 ModelPort](ADR-0002-model-port.md) | 模型边界理由；当前含原生与 LangChain 两个 DeepSeek 适配器 |
| [0003 Runtime／求职领域分离](ADR-0003-runtime-domain-separation.md) | 领域边界；不表示已有求职业务应用 |
| [0004 CLI 应用层](ADR-0004-cli-application-layer.md) | 应用／终端分离；当前入口见 CLI 模块 |
| [0005 产品仓库启动链](ADR-0005-agent-context-free-startup.md) | 仓库边界继续适用；2026-11-11 明确当前行为以代码为准 |
| [0006 Milvus](ADR-0006-milvus-standalone-knowledge-index.md) | 历史索引方向，真实配置与验证仍有缺口 |
| [0007 Context 与 Memory](ADR-0007-context-projection-memory.md) | 原 Proposed 状态保留；已有实现见 Context、Memory、Session 模块 |
| [0008 LangChain／LangGraph](ADR-0008-langchain-langgraph-boundaries.md) | 框架边界已体现在代码中；默认 native/native，无持久图恢复 |
