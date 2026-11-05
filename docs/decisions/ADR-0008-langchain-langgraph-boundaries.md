# ADR-0008 LangChain 与 LangGraph 的接入边界

状态：`Approved`（2026-11-05 所有者明确选择自建 StateGraph 等方案并要求实施）。

初稿日期：2026-10-29；R1 修订及实施授权日期：2026-11-05。实现状态见
[实施记录](../plans/v0.6-implementation.md)；批准决策不等于工程验收通过。

## 背景

JDAgent 已实现模型端口、顺序工具 Runtime、事件持久化、上下文压缩和版本化记忆。
v0.6 希望用 LangChain 与 LangGraph 减少自行维护的模型接入和流程控制代码，同时保留已有产品行为。
直接使用预置 Agent 和框架存储会同时改变主循环、消息历史、工具权限、费用与恢复，迁移范围过大。

## 提议决策

- LangGraph 的自建 StateGraph 负责一轮 Agent 的执行顺序和条件分支，通过项目 TurnRunner
  用例接入 Coordinator。框架 state 和异常不成为外部应用合同。
- LangChain 模型组件实现现有 ModelPort，首个后端为 ChatDeepSeek。SDK 消息、chunk、异常
  和配置转换封装在 Adapter 内，领域类型和 Session 仍使用项目合同。
- ContextService、ToolRuntime、EventJournal、SessionRecovery、MemoryStore 和 Knowledge
  服务保持自身职责；图节点组合这些能力，不复制业务规则或建立第二套事实记录。
- 工具保持批次内顺序执行和既有审批；不用默认 ToolNode 接管执行，不使用 create_agent
  再维护第二个主循环，不为副作用节点启用自动重试。
- v0.6 不启用持久图 checkpointer 或 interrupt，继续通过 Session 事实恢复。图状态只在
  当前执行中有效；不得宣传未实现的图持久恢复或外部副作用恰好一次保证。
- 模型适配器与执行引擎分阶段迁移，使用相同公共接口做差分验证。后端只在重启并完成恢复检查后
  选择，失败时不自动切换引擎重跑同一 Turn。
- 后端选择在进程内固定，现有模型和请求设置仍可逐轮重载。每份 ModelRequest 决定本次发送参数；
  复用客户端不改变已建立请求的快照。跨响应／历史调用 ID 检查及业务工具计数仍由 Runner 负责，
  不能委托给只看单次请求的 Adapter，或改用框架执行步数代替。
- 两种引擎与两种适配器的四种组合都要有真实接线的离线证据。Live 总预算包装作用于选定的
  ModelPort，不绑定旧适配器类；新旧 Adapter 均通过发送前阻断验证后，才执行有明确额度的对照。

## 与已有决策的关系

[ADR-0001](ADR-0001-python-core.md)、[ADR-0002](ADR-0002-model-port.md)、
[ADR-0003](ADR-0003-runtime-domain-separation.md)、[ADR-0004](ADR-0004-cli-application-layer.md)
和 [ADR-0006](ADR-0006-milvus-standalone-knowledge-index.md) 继续有效。
ADR-0002 当时为了学习协议机制没有先采用通用框架；本提案调整 Adapter 的实现选择，保留其
ModelPort 和类型隔离决策，不直接以框架消息取代领域模型。

[ADR-0007](ADR-0007-context-projection-memory.md) 仍为 Proposed，本文不替它改变批准状态。
v0.5 已确认的产品行为和当前实现是迁移输入；它们的工程与学习验收状态保持原记录。

## 备选方案

仅引入 LangChain 对模型接入有效，但不能完成本次主流程改造，可作为 P1 中间态。
由 create_agent、middleware 和框架 persistence 整体接管，可减少新建 Agent 的基础代码，
但需要重新证明当前更细的权限、上下文、费用与未知副作用合同，暂不适用于本次等价迁移。

## 后果

主流程变得显式，Provider 接入可复用生态，现有业务服务与测试继续发挥作用。
代价是新增框架及传递依赖、两层消息转换和迁移期双实现。版本必须锁定，默认重试与端点行为
必须实测，旧实现后续应按证据退出，避免长期重复维护。

没有持久图 checkpoint 的首版不能在任意节点恢复，也没有跨进程持久审批。
会话继续、原文保留和未知副作用隔离由已有机制提供，而不是由未启用的图功能提供。

## 复审条件

出现已批准的跨进程暂停／继续、多 Agent、并行工具或远程执行需求时，复审图持久化与审批。
若 LangChain Adapter 无法保留必要协议、费用或取消证据，阻止默认切换，重新比较具体集成方式。
若实测只有抽象层增加而控制代码、可测试性或维护范围没有改善，应重新评估图边界。

完整需求、数据流、迁移和验收见 [v0.6 设计](../architecture/v0.6-langchain-langgraph.md)，
框架事实依据见[官方资料](../research/v0.6-langchain-langgraph.md)，
R1 修订范围及尚待实施的验证见[自审处置](../reviews/REV-20261105-001-v0.6-design-disposition.md)。
