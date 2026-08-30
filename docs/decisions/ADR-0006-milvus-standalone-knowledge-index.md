# ADR-0006：Milvus Standalone 承载 v0.3 知识索引

状态：`Approved`

日期：2026-08-28

批准日期：2026-08-30

## 背景

v0.3 要让 JDAgent 管理来源摄取、不可变 Source Version、检索和来源可追踪回答，同时使用具有
真实生产路径的 Dense、BM25、Hybrid Search 与 HNSW 能力。Milvus Lite 适合本地原型，但不能
验证选定的 HNSW 路径；Milvus Distributed 又会把当前单用户、低并发模块扩大成分布式运维项目。

数据库不能成为知识事实的隐式所有者。否则 Collection Schema、Milvus 返回类型和服务可用性会
同时侵入来源生命周期、Session、Context 注入与 Citation，索引损坏也会被错误表示成知识丢失。

## 决策

- v0.3 的 JDAgent-managed Knowledge Base 由 JDAgent 负责摄取、Source Version 和 Knowledge
  Index 生命周期；Milvus Standalone 是首个外部 Knowledge Index 存储与检索服务。
- Source Version 是知识事实，Knowledge Index 是可从 Source Version 重建的派生投影。Milvus
  不拥有 Source Version，也不成为 Session Event 或回答来源的事实源。
- Knowledge Catalog 与 Source Store 按关联高层架构分别拥有来源身份/生命周期和不可变内容对象；
  Milvus 两者都不拥有。Parser 派生版本与删除墓碑的精确语义由高层架构统一定义，不在本 ADR 复制。
- Knowledge Index 分为 Index Generation 与 Content Revision。解析、切分、Embedding、字段 Schema、
  Retrieval Language Profile 或关键索引配置破坏兼容性时，构建并验证新的 Generation；来源新增、
  替换、停用和逻辑删除只产生新的 Content Revision。检索按 Turn 冻结的 Generation 与 Revision
  执行，更新中的内容不可见。
- Catalog 中的 Generation/Revision 和 Adapter 私有物理 Collection 映射是查询唯一事实源。Runtime
  不通过 Milvus alias 选择目标；每个 Dense 与 BM25/Sparse 请求在 top-K 前应用冻结 Revision
  Filter。Reconciler 只能把 alias 单向调整到 Catalog，不能反向改写 Catalog。
- JDAgent 通过项目自有的检索与 Evidence 类型调用索引用例；Milvus Collection、Hit、Score、
  Filter 和 SDK 异常只存在于 Adapter 边界内，不进入 Agent Core、Session 或 ModelPort。
- 文档与查询向量化使用独立 EmbeddingPort，可选 Cross-Encoder 使用独立 RerankerPort；二者不复用
  聊天生成 ModelPort。v0.3 首个真实 Embedding Adapter 使用 OpenAI-compatible HTTP 合同，具体
  Provider 和模型由版本化 Embedding Profile 指定，基础安装不自动下载本地模型。
- v0.3 首先实现 JDAgent-managed Knowledge Base。该边界允许未来真实的 MCP 或外部 Knowledge
  Provider 转换为同一 Evidence 合同，但本决策不预创建外部 Provider Adapter、插件框架或空壳
  实现。
- Milvus 不可用不阻止 JDAgent 本体启动。任一 Workspace Binding 指向的知识库无法进入或完成
  检索时，必须保留其身份并表示为 `Knowledge Unavailable`，不能伪装成空结果或
  `Insufficient Evidence`。完整的 Binding、PTK、Outcome、Citation 与恢复合同由关联高层架构拥有。
- 每个 Knowledge Base 显式使用版本化 Retrieval Language Profile；索引和查询使用同一 Profile。
  v0.3 默认 Profile 为 `mixed_zh_en_v1`，精确 Analyzer 配置必须通过 Analyzer 检查和 Retrieval Eval
  固化，不使用按来源自动语言识别。
- v0.3 不承诺 Milvus 高可用、多用户并发、分布式部署或延迟 SLA。生产级目标是当前单用户、低并发
  范围内的版本、幂等、恢复、错误、可观察性、可重建性和来源正确性。

## 备选方案

### Milvus Lite 作为默认部署

本地运行和测试更轻，但无法验证已经选择的 HNSW 索引路径，也会弱化真实外部服务故障、Schema
兼容和恢复行为的学习目标，因此不作为 v0.3 默认部署。

### Milvus Distributed 作为默认部署

可以更早验证分布式伸缩和高可用，但这些不是当前产品结果或验收目标，会引入 Kubernetes、容量、
多租户和运维边界，拒绝进入 v0.3。

### 让 Milvus Collection 成为知识库事实

实现更直接，但来源版本、删除替换、Citation 和恢复都会依赖数据库内部 Schema，并使索引丢失等同
于知识丢失，因此拒绝。

## 后果

- 使用 RAG 能力需要可连接的 Milvus Standalone；开发、安装和集成验证必须覆盖服务启动、Schema
  兼容、连接失败和索引重建。
- JDAgent 需要按关联高层架构使用用户级内容寻址 Source Store，独立保存足以重建索引并解析
  Citation 的来源事实；Milvus 备份不能替代这些事实。
- Knowledge Catalog 损坏时，知识模块失败关闭但 JDAgent 本体继续运行；内容寻址对象不能自动重建
  来源身份、授权或激活关系。恢复依赖已验证的 Catalog 备份或显式重新导入，Reconciler 只处理
  合法 Catalog 中未完成的 Saga。
- Embedding、BM25、RRF、HNSW 和 Reranking 的配置必须通过 Index Generation、Content Revision
  和 Retrieval Profile fingerprint 形成可审计身份，不能只存在于 Milvus 的隐式状态中。
- 摄取跨 SQLite Catalog、内容寻址文件和 Milvus 时使用显式 Saga、稳定操作 ID、幂等重试与
  Reconciler；只有通过验证并完成激活的 Generation/Revision 可见，不能声称不存在的跨存储原子事务。
- 未来替换 Milvus 或接入外部 Knowledge Provider 时，调用方继续消费项目自有 Evidence 合同，
  但迁移工具和 Provider Adapter 只在真实需求出现时实现。

## 复审条件

- Milvus Standalone 无法在支持环境中达到已批准的正确性、恢复或 Retrieval Eval 门禁。
- 产品要求无外部服务的离线 RAG，且 Milvus Lite 或其他嵌入式索引能够满足批准的检索路径。
- 出现真实 MCP 或外部 Knowledge Provider，需要验证现有 Evidence 边界是否足够。
- 产品进入多用户、高并发、高可用或分布式部署阶段。
