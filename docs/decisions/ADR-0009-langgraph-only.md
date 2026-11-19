# ADR-0009 · LangGraph 统一调度每个 Turn

状态为 `Implemented`。项目所有者于 2026-11-19 选定单一执行引擎。

原先每次改变响应结束、工具排序或取消规则，都要在原生循环与图执行器对照实现。
v0.8 从组合根直接构造 LangGraph 执行器。应用层依赖 TurnRunner 合同，模型适配器把
DeepSeek 或 Fake 转成项目事件。工具模块继续负责权限、执行和清理。恢复时读取会话日志和对象。

这一决策接替 [ADR-0008](ADR-0008-langchain-langgraph-boundaries.md) 中双引擎选择与四组合验证条款。
项目保留独立的模型与工具协议、顺序工具调用、无持久图 checkpoint、无节点自动重试和追踪隔离。
常规回归在同一张图上分别使用原生和 LangChain 两个模型适配器。旧引擎字段在 v0.8 输入边界提示移除。

代价是每轮承担构图和调度开销，回退需要切换版本。收益是从 CLI 到终态只有一条可追踪的调用链。
[规格与验收条件](../plans/v0.8-langgraph-turn-runner.md) · [实测与回退](../development/v0.8-langgraph-verification.md)
