# 当前实现术语

状态：`Implemented`。核对日期：2026-11-11，代码基准：`e23604f`。

| 术语 | 在当前代码中的含义 |
| --- | --- |
| Workspace | 已存在的目标目录；规范绝对路径的 SHA-256 决定项目数据身份 |
| Session | 追加事件组成的持久对话，由 OS 独占锁保护，可包含多轮输入 |
| Turn | 一次普通输入或直接 Prompt 所触发的执行，可多次调用模型与工具 |
| TurnRunner | 一轮调度的接口，由 AgentLoop 或 LangGraphTurnRunner 实现 |
| ModelPort | 统一请求与异步模型事件流的接口；适配器负责 Provider 协议转换 |
| Runtime Event | 已保存的用户、模型、工具、权限、费用和终态等事实 |
| Context | 某次模型请求实际看到的消息、指令、工具和证据，不是完整 Session 文件 |
| RequestManifest | 已发送请求的逻辑清单及对象／知识引用，用于重建和核对 |
| Compaction / checkpoint | 用摘要和引用形成新历史投影；checkpoint 提交后生效，原文仍保留 |
| Memory | 独立的版本化陈述，含作用域、状态和来源；向量不替代原记录 |
| Revision（文件） | 完整文件字节与规范路径生成的编辑前提，防止旧内容覆盖新文件 |
| Knowledge Catalog | 连接、知识库、绑定、来源版本与操作状态的 SQLite 元数据 |
| Knowledge Connection / Binding | 用户登记的连接，以及 workspace 获准使用其知识库的关系 |
| Source / Source Version | 一份逻辑资料的身份及其内容版本；原始文件只是导入输入 |
| Source Store | 原始输入字节与解析快照的内容寻址对象存储 |
| Index Generation / Content Revision | 索引代次和该代次下的内容可见视图；不是会话或文件 revision |
| PreparedTurnKnowledge | 一轮开始时冻结的知识范围、检索结果和证据，后续模型调用复用 |
| Evidence / Citation | 本轮提供给模型的材料，以及回答对材料的定位关系；定位有效不证明语义支持 |
| MCP Server / capability | 一次远端连接及其 tool、resource、resource template、prompt 定义 |
| MCP generation / definition digest | 目录代次与定义摘要，用于校验本次请求实际曝光的工具 |
| MCP Task / task_ref | 远端长任务及本地受控引用；远端私有句柄不作为模型可见引用 |
| ToolOutcome | 分开记录完成、副作用、观察到失败、取消及是否必须停止的执行结果 |
| unknown 副作用 | 已开始但无法确认结果的本地写入／命令，不等于未执行 |

深入理解按[架构总览](../architecture/overview.md)进入对应代码。RAG 早期完整概念与目标见
[历史词汇表](../archive/2026-11-11/context/v0.3-rag-glossary.md)；其中生命周期承诺不代表全部已经实现。
