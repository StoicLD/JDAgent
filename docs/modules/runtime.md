# 运行时与主循环

状态：`Implemented`（实现说明）

本模块负责把一次用户输入推进到回答、失败或取消。基准与验收范围见[Map](../map.md)、
[验证与验收](verification.md)。

## 职责与边界

外层 `InteractiveApplication` 等待输入、处理命令，再调用 `TurnCoordinator.send()`。
Coordinator 打开 Session、检查恢复状态、记录用户输入、准备本轮知识，随后通过 `TurnRunner` 执行本轮。
组合根默认创建原生 `AgentLoop`；v0.6 开发阶段也可显式选择 `LangGraphTurnRunner`。
一个 Session 包含多个 Turn，一个 Turn 可以调用模型和工具多次。

Runner 决定调用顺序和停止条件；`ContextService` 在请求前执行预算、归档与压缩，
内部使用 `ContextBuilder` 构造基本消息；
`ModelPort` 接模型；`ToolRuntimePort` 执行工具；`RuntimeJournal` 保存事实。
具体依赖由 `composition.py` 组装。Loop 不直接拼 DeepSeek HTTP 请求或读写 JSONL。

## 一轮的执行过程

```mermaid
flowchart TD
    A["Coordinator 记录输入并准备知识"] --> B["检查取消和模型调用预算"]
    B --> C["从事件构造上下文"]
    C --> D["接收模型流式响应"]
    D --> E{"是否有完整工具调用"}
    E -- 有 --> F["保存 assistant 消息"]
    F --> G["依次执行工具并保存结果"]
    G --> B
    E -- 无且正常停止 --> H["校验引用 必要时修复一次"]
    H --> I["保存回答与 turn_completed"]
    G -. "必须停止的结果" .-> J["记录 turn_failed"]
    B -. "取消或预算耗尽" .-> J
    C -. "上下文超限" .-> J
    D -. "超时或协议错误" .-> J
```

Loop 接收完整响应后才执行工具；同批多个调用按顺序 `await`，没有并行工具调度。
每次重新调用模型都读取最新事件投影，因此模型能看到前一次的工具结果。
工具参数错误、权限拒绝等通常作为结果返回模型；未知副作用或要求停止的结果会结束 Turn。

模型必须以匹配的 `finish_reason` 结束：有工具调用时为 `tool_calls`，无工具时为 `stop`。
不完整流、重复调用 ID、不匹配的结束原因都不会作为成功回答。
最终引用修复最多一次、禁用工具，计入模型预算；具体引用语义见[知识模块](knowledge.md)。

## 控制与完成含义

默认每轮最多 16 次模型调用、32 次工具调用。`max_failure_recoveries=2`：
Loop 累计 `ToolOutcome.observed_failure`，第三次观察到失败时停止，避免无限修复。
启动失败没有验证结果，不能等同于“测试失败”。

停止原因包括 `completed`、`cancelled`、`model_error`、`context_limit`、
`limit_reached`、`session_error`、`internal_error`。持久化失败等边界错误还由应用层处理。
取消期间工具运行层先等待自己拥有的任务清理；中断批次的配对处理见[会话模块](sessions.md)。

`completed` 表示模型正常结束且没有待执行调用，不证明用户的所有任务要求已满足。
当前没有通用任务验收器、独立 Planner/Executor 或多 Agent 调度。

## 采用的设计

行为上是 ReAct 风格的“判断 → 工具动作 → 观察结果”闭环，动作使用结构化 Function Calling。
原生引擎使用 `while`、分支和预算计数器。新增图引擎使用实际 StateGraph 节点和条件边，分别负责
请求准备、模型调用、引用校验与修复、保存回答、逐项工具执行和成功／失败收尾。
图引擎通过 `--runtime-engine langgraph` 选择；两者遵循同一 `TurnRunner` 合同。
模块通过端口与适配器实现依赖倒置，状态以事件保存、按用途投影。

图每次运行创建独立状态；无 checkpointer、节点重试、缓存或并行工具。图步数保护独立于模型和工具
业务限额。失败先补齐待决工具结果再记录终态；已保存终态后的观察者异常不会生成第二个终态。
持久化错误向调用方传播，任务取消等待模型流或工具的清理。
工具 ID 集合在 Turn 开始时从历史初始化，按工具发起时点更新；发现目录规则时，未执行的后续调用
仍占用该批次的工具次数。这些语义保留原循环行为。

v0.6 尚在实施，当前默认仍为原生引擎；完整验收范围及剩余项见[实施记录](../plans/v0.6-implementation.md)。

早期“小而稳定的 Loop”是设计方向；现有 Loop 已直接包含知识引用校验与修复分支，
Coordinator 也包含知识 Catalog／Index 组装。后续讨论模块边界时应以这些实际依赖为起点。

## 代码与验证入口

- [Loop 与预算](../../src/jdagent/core/loop.py)、[Coordinator](../../src/jdagent/application/coordinator.py)
- [共享执行合同](../../src/jdagent/core/contracts.py)、[StateGraph 引擎](../../src/jdagent/runtime/langgraph_runner.py)、
  [引擎差分和故障用例](../../tests/test_v06_graph_runner.py)
- [端口](../../src/jdagent/ports.py)、[组合根](../../src/jdagent/composition.py)
- [主循环测试](../../tests/test_agent_loop.py)、[运行控制测试](../../tests/test_runtime_controls.py)、
  [v0.4 合同测试](../../tests/test_v04_contracts.py)
- 设计来源：[早期 Runtime 架构](../archive/v0.1/agent-runtime.md)、
  [ADR-0002](../decisions/ADR-0002-model-port.md)、[ADR-0003](../decisions/ADR-0003-runtime-domain-separation.md)
