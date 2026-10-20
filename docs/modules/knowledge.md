# 知识摄取与检索

状态：`Implemented`（现有 P1 实现说明，工程验收未通过）

知识模块已有摄取、索引、按 Turn 检索和引用处理链路，但真实配置与数据生命周期仍有缺口。
基准见[Map](../map.md)；概念定义见[词汇表](../context/v0.3-rag-glossary.md)，
入门示例见[休假政策图解](../context/v0.3-rag-illustrated-guide.md)。

## 状态和内容归属

| 对象 | 当前职责 |
| --- | --- |
| Knowledge Catalog | 保存连接、知识库、Binding、来源与版本、Generation／Revision 和操作状态等元数据 |
| Source Store | 按内容哈希保存原始字节及解析快照对象 |
| Knowledge Index | 保存用于搜索的 Chunk、向量与可见区间，是派生投影 |
| Workspace Binding | 指定 workspace 获准使用哪些连接下的知识库，不属于 Session |
| PreparedTurnKnowledge | 本轮冻结的检索范围、结果和证据，供本轮所有模型调用复用 |

已批准设计要求这些身份和所有权分开；现有实现仍有部分 Snapshot／Generation 独立生命周期缺口，
不能仅凭类型或字段存在认定完整设计已经实现。

## 摄取链

`KnowledgeIngestion.add_file()` 读取来源，解码 TXT／MD／CSV，解析结构与 Locator，
进行 Parent/Child 切分，保存内容对象、版本元数据和索引记录，再推进可见 Revision。
Embedding 根据 Profile 使用 HTTP 适配器或离线 HashEmbedding。
Chunk 身份包含 Source Version，避免不同来源的相同内容互相覆盖。

Catalog 保存操作阶段与 Lease，提供 retry／reconcile；来源可停用、重新启用或删除。
这些入口已有实现，但崩溃恢复、激活事务和删除终态尚未完整满足目标，见下表。
本地 File Index 用于离线路径；Milvus 为已批准的真实索引方向，按需加载 rag extra。

## 一轮检索与回答

1. Coordinator 根据 workspace Binding 解析有效知识库，冻结 Generation、Revision 和 Profile。
2. `TurnKnowledgePreparation` 按库检索，组合关键词与向量候选，使用 RRF 合并排名。
3. 按候选展开 Parent，去重并按预算选择 Evidence，记录逐库状态、指纹与检索预算。
4. ContextBuilder 将本轮 Evidence 注入请求；Loop 中的后续工具调用不会重新检索知识。
5. 最终回答解析带 Turn 身份的机器引用，形成 Citation 和模型补充；非法引用最多修复一次。

无 Catalog／Binding 的普通对话返回 `not_configured`，不会为普通问答创建 Catalog。
检索不可用、部分失败与证据不足有独立状态。引用有效只表示定位关系有效，
不能据此断言证据支持回答中的每个主张。

## 已确认限制和后续工作

以下编号来自 [2026-09-05 审查](../reviews/REV-20260905-001-v0.3-implementation-audit.md)；
该报告中的历史结论与后续修复应合并阅读。

| 范围 | 当前限制 |
| --- | --- |
| F08 恢复 | 缺失索引写入不能全部从持久 Manifest 自动重建；KB 激活与 Source 指针仍分步提交；Lease 续期和早期重试输入稳定性不足 |
| F09 删除 | 墓碑排除新检索，但索引物理 purge 与幂等 Delete Saga 尚不完整 |
| F10 来源核验 | 历史 Citation Resolver 未完整接入真实入口；Parent 文本仍来自 IndexHit，缺少完整 Store 加载与哈希核验 |
| F11 真实配置 | CLI 未完整暴露 Embedding/Profile 和 credential_ref；默认 Runtime 不读取 CLI 的本地索引 |
| F12 Milvus | BM25 异常时仍可能回退本地词频评分；SDK 错误映射与实际版本兼容矩阵未完成 |
| F13 跨库预算 | 全局预算仍受最后一个库的 Profile 影响；Generation 与物理 Collection 映射未充分分离 |
| F14 Eval | 词项 Recall 未达门槛；回答支持评测仍主要是目标／Locator 代理，不能当作主张语义支持 |

正常组合根的 `RuntimeOptions.allow_local_index=False`；
离线测试显式开启本地索引不等于用户默认 CLI 的真实 RAG 路径已打通。
已有 HTTP Embedding 类也不等于凭证与 Profile 的用户配置链已完成。

原 F15 的 Citation repair 预算、取消、异常、Usage 和 finish_reason 控制已在 v0.4 处理，
依据[实施审查 F12 处置](../reviews/v0.4-implementation-review.md)与当前 Loop。
这不代表历史引用打开、supplement 引用核验或整个 RAG 工程验收已完成。
此外，独立 Snapshot 派生、完整 Generation 重建／GC、长 I/O 隔离等缺口仍见原审查。

## 设计与验证入口

RAG 高层设计和阶段计划已批准；M0 详细需求、知识合同与 Eval 规格仍为 `Proposed`。
[改进方案](../plans/v0.3-rag-improvement-proposal.md)也是 `Proposed`，不构成新实施授权。
工程未通过与学习出口已关闭分别记录在[验证模块](verification.md)。

- [Catalog](../../src/jdagent/knowledge/catalog.py)、[Store](../../src/jdagent/knowledge/store.py)、
  [摄取](../../src/jdagent/knowledge/ingestion.py)、[解析](../../src/jdagent/knowledge/parsing.py)
- [索引](../../src/jdagent/knowledge/index.py)、[Milvus](../../src/jdagent/knowledge/milvus.py)、
  [Embedding](../../src/jdagent/knowledge/embedding.py)
- [Turn Preparation](../../src/jdagent/knowledge/preparation.py)、[引用](../../src/jdagent/knowledge/citation.py)
- [摄取测试](../../tests/test_ingestion.py)、[来源回归](../../tests/test_source_revision_regressions.py)、
  [检索测试](../../tests/test_retrieval.py)、[引用测试](../../tests/test_citation.py)、
  [Eval](../../tests/test_v03_eval.py)
- [已批准高层设计](../architecture/v0.3-knowledge-retrieval.md)、
  [阶段范围](../plans/v0.3-rag-p1-p2-appendix.md)、[人工验证步骤](../development/v0.3-rag-manual-verification.md)
