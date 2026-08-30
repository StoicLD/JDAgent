# v0.3 知识 Port、Payload 与 Runtime Event v2 合同

状态：`Proposed`

日期：2026-08-30

关联需求：[v0.3 需求](../requirements/v0.3-knowledge-retrieval.md)

关联架构：[高层设计](v0.3-knowledge-retrieval.md)

关联 Saga：[Milvus / Revision / Saga](milvus-revision-saga-v0.3.md)

本文固定 M1 起必须遵守的公开合同。类型名描述职责与字段，不强制等于某个 Python 文件或
`Protocol`。只有行为在生产 Adapter 与 Fake 之间变化时才建立 seam。实现者可调整模块组织，但不得
改变字段语义、时序、所有权或失败分类。

## 1. 标识与指纹

- 业务 ID 为非空 ASCII 稳定字符串，创建后只读。建议前缀：`conn_`、`kb_`、`src_`、`sv_`、
  `snap_`、`bind_`、`op_`、`gen_`、`ev_`。
- Knowledge Base Reference = `(connection_id, knowledge_base_id)`，不含 Endpoint、Collection、
  凭证。
- Embedding Profile fingerprint：对
  `{provider, base_url, model, dimension, normalize, extra}` 做 canonical JSON SHA-256。
- Retrieval Language Profile fingerprint：对 Analyzer 名称、词典版本与 Filter 配置做同样哈希。
- Retrieval Profile fingerprint：mode、RRF k、预算、以及所引用的 Embedding/Language fingerprint。
- Parser Profile fingerprint：parser 名称与版本、结构规则。Locator Schema 版本为独立整数，v0.3
  起始为 `1`。
- 内容哈希：SHA-256，十六进制小写；对象键由其派生。

## 2. Knowledge Use Cases

CLI token 映射只在 P1.11 维护。Application 动词如下；全部变更命令在副作用前完成校验。

### 2.1 Connection

| Use Case | 输入 | 成功结果 | 稳定失败 |
| --- | --- | --- | --- |
| `register` | name, provider_kind=`jdagent_managed`, endpoint?, credential_ref?, embedding_profile? | connection_id | 名称冲突、非法 ref |
| `test` | connection_id | 脱敏连通性（M1 只校验记录完整性，不 Ping Milvus） | 缺失、凭证不可解析 |
| `list` | 无 | 摘要列表，无秘密 | Catalog 损坏 |
| `remove` | connection_id, confirm | 删除；若仍被 KB/Binding 引用则拒绝 | 缺失、仍被引用、未确认 |

`credential_ref` 语法仅允许 `env:<NAME>` 或 `file:<absolute-path>`。解析值不得进入返回对象。

### 2.2 Knowledge Base

| Use Case | 输入 | 成功结果 | 稳定失败 |
| --- | --- | --- | --- |
| `create` | connection_id, name, embedding_profile, retrieval_profile, language_profile=`mixed_zh_en_v1` | kb_id | 名称冲突、connection 缺失 |
| `list` | connection_id? | 摘要 | Catalog 损坏 |
| `status` | kb_id | 生命周期、当前 generation_id、revision、来源计数 | 缺失 |
| `delete` | kb_id, confirm | 进入删除 Saga | 未确认、Lease 忙 |

### 2.3 Binding

| Use Case | 输入 | 成功结果 | 稳定失败 |
| --- | --- | --- | --- |
| `bind` | workspace_identity, connection_id, kb_id | binding_id | 重复绑定、引用不存在 |
| `unbind` | binding_id 或 (workspace_identity, kb_id), confirm | 删除授权 | 缺失、未确认 |
| `list` | workspace_identity | 该 Workspace 的绑定 | Catalog 损坏 |

无效 Binding 在 Turn 冻结解析前归类为 `BINDING_INVALID`，不在 bind 时抢先删除。

### 2.4 Source 与 Operation（M3 起）

`add` / `replace` / `deactivate` / `reactivate` / `delete` / `status` / `retry` / `reconcile`
的状态机见 Saga 合同。Headless 破坏性操作必须 `--yes`。

## 3. Catalog 与 Store 边界

### 3.1 Knowledge Catalog

职责：身份、关系、生命周期、Saga、Lease、激活指针、Adapter 私有物理映射。

打开合同：

1. 普通 Agent 启动不打开 Catalog。
2. 仅当执行知识 Use Case，或 Turn 存在 Binding 需要解析时打开。
3. 打开时执行轻量 `PRAGMA quick_check`；非 `ok` 则 `CATALOG_CORRUPT`，禁止 GC 与 Mutation。
4. Schema 版本存储于 `catalog_meta`；升级前 SQLite Online Backup 到 backups 目录并
   `PRAGMA integrity_check` 通过。
5. 变更日后默认每日一份备份，保留 7 份。

Catalog 表最小集合（逻辑，非强制 DDL 原文）：

- `connections`、`knowledge_bases`、`bindings`
- `sources`、`source_versions`、`parsed_snapshots`
- `index_generations`（含 `physical_collection`）、`content_revisions`
- `chunk_revisions`（`valid_from_revision`, `valid_to_revision` 可空）
- `operations`、`leases`、`catalog_meta`

不保存：秘密值、Evidence 全文、模型回答、Query 正文。

### 3.2 Source Store

职责：按哈希保存 Raw 与 Parsed 字节。

写入：`temp file → 完整 write → flush → fsync → atomic replace`；读时校验哈希。相同哈希幂等。
Catalog 只在对象持久化成功后引用。Store 不解释 Source 是否 ACTIVE。共享对象仅当 Catalog 证明零
引用且超过保留期后由 GC 删除；Catalog 损坏时禁止 GC。对象缺失 → 来源损坏，不是 Delete。

单对象上限 50 MiB；超过则该文件失败，不截断。

## 4. Embedding、Index、Provider、Reranker

### 4.1 EmbeddingPort

请求：`texts: tuple[str, ...]`、`profile: EmbeddingProfile`、`input_kind: document|query`。

响应：`vectors: tuple[tuple[float, ...], ...]`、`model_identity`、`dimension`。

校验：长度等于输入；每向量维度等于 Profile；全部有限浮点。超时默认 30s；可重试网络/429 最多 2
次。不得复用 DeepSeek 聊天配置。Fake 必须走同一请求/校验路径，用确定性向量（对规范化文本做
稳定哈希展开到 dimension）。

### 4.2 Knowledge Index Port

隐藏 Milvus Collection/Hit/Score/Filter/SDK。项目自有操作：

- `upsert_chunks(generation_id, chunks, revision)`
- `search_dense(generation_id, revision, vector, top_k, filter_same_as_query)`
- `search_bm25(generation_id, revision, query_text, top_k)`
- `drop_generation(generation_id)`（仅 GC）

每个 search 必须在 ANN/关键词 top-K **之前**应用冻结 Revision Filter：

```text
valid_from_revision <= frozen_revision
AND (valid_to_revision IS NULL OR valid_to_revision > frozen_revision)
```

禁止先取未过滤 top-K 再在应用层丢弃。Catalog 记录的 `physical_collection` 是唯一目标；alias 不
参与 Search。缺失物理资源 → `PROVIDER_UNAVAILABLE`。

### 4.3 Knowledge Provider

对单个冻结 Knowledge Base：接受已计算的 Query Vector（若 mode 需要）与冻结目标，返回
`hit_count`、候选 Child、或 typed failure。不执行 Embedding、跨库融合、全局 Rerank、Parent
Expansion。成功空结果 `status=SUCCEEDED` 且 `hit_count=0`。

### 4.4 RerankerPort

可选。一次 Turn 最多调用一次，作用于跨库融合后的全局候选。失败：返回精排前顺序，记录
`reranker_fallback` degradation。v0.3 默认不配置真实模型；Fake 用于合同测试。

## 5. Prepared Turn Knowledge

PTK 是当前开放 Turn 的内存不可变值，不写入 Session。

```text
PreparedTurnKnowledge
  turn_id
  turn_token          # 短、不可与其他 Turn 碰撞；用于机器 Reference
  outcome             # NOT_CONFIGURED|COMPLETE|PARTIAL|UNAVAILABLE|INSUFFICIENT
  query_fingerprint   # 对原始 Query 的哈希，Trace 可用；正文不入 Trace
  bases[]             # 逐 Binding 结果
  evidence[]          # 预算后最终 Parent Evidence，有序
  budget              # 候选/Rerank/Evidence token 与 Parent 计数
  degradations[]      # 如 reranker_fallback
  retrieval_profile_fingerprint
```

Evidence 至少包含：`evidence_id`、`reference`（`K:<turn_token>:E<n>`，n 从 1）、`kb_id`、
`source_id`、`source_version_id`、`snapshot_id`、`locator`、`locator_schema_version`、
`content_hash`、`parent_text`、`token_estimate`。

Outcome 穷尽规则（B=Binding 数，S=成功查询数含空结果，F=失败数，E=最终 Evidence 数）：

```text
B = 0                 → NOT_CONFIGURED
B > 0 and S = 0       → UNAVAILABLE
S > 0 and F > 0       → PARTIAL
F = 0 and E > 0       → COMPLETE
F = 0 and E = 0       → INSUFFICIENT
```

单库失败原因仅四值：`BINDING_INVALID`、`ACCESS_DENIED`、`PROVIDER_UNAVAILABLE`、`QUERY_FAILED`。
解析前不存在用 `BINDING_INVALID`；解析成功后再消失按阶段映射后两者，不得回写冻结快照。

### 5.1 预算顺序与 P1 起始值

顺序：候选生成 → 库内融合 → 跨库融合 → 可选 Rerank → Parent Expansion → 最终 Token 裁剪。

起始值：每库 Dense 20、BM25 20、库内 RRF 留 20 Child；跨库后最多 40 Child 进 Rerank；最终最多
6,000 tokens、8 个 Parent、单 Parent 2,000 tokens。Token 估计与 ContextBuilder 相同：
`max(1, (len(text) + 3) // 4)`。超预算的 Parent 从队尾丢弃，不把半个 Parent 注入模型。

### 5.2 跨库融合

等权 Reciprocal Rank Fusion，`k=60`。不比较原始分数。同一 `(source_version_id, locator)` 在全局
阶段去重，保留更靠前的排名。某库无命中不算失败。

## 6. Runtime Event Schema v2

新 Session 从第一条事件起 `schema_version=2`。JSONL 仍一行一对象、UTF-8、严格递增 sequence。

### 6.1 相对 v1 的变化

| 变化 | 合同 |
| --- | --- |
| 新事件 `turn_retrieval_recorded` | 首次模型调用前必写 |
| `assistant_message_completed` | 增加 `citations` 与 `model_supplement` |
| `turn_failed.error_category` | 允许 `invalid_citation`、`knowledge_preparation_cancelled`、`process_interrupted` |
| Reader | `schema_version==1` → `unsupported_schema`（可 resume 拒绝，不当损坏尾） |
| Reader | `schema_version` 不是 2 → `unsupported_schema` |
| Recovery | 未知 Schema 的完整行是 `UNRECOVERABLE`，不得当残尾截断 |

v1 事件类型集合保持可解析为“不受支持的旧 Session”，不必把 v1 payload 映射进 v2 领域对象。提供
显式清理说明或命令，启动时不自动删除旧文件。

### 6.2 `turn_retrieval_recorded` payload

不含 Citation，不含 Evidence 全文。

```text
outcome: str
turn_token: str
bases: [{
  binding_id, connection_id, knowledge_base_id, kb_name,
  status: SUCCEEDED|FAILED,
  failure_reason: null | BINDING_INVALID|ACCESS_DENIED|PROVIDER_UNAVAILABLE|QUERY_FAILED,
  generation_id, revision, physical_collection,
  embedding_profile_fingerprint, retrieval_profile_fingerprint,
  hit_count, selected_evidence_count
}]
evidence: [{
  evidence_id, ordinal, knowledge_base_id, source_id, source_version_id,
  snapshot_id, locator, locator_schema_version, content_hash
}]
budget: {dense_top_k, bm25_top_k, fused_child_count, rerank_pool_size,
         parent_count, evidence_tokens, evidence_token_limit, parent_limit}
degradations: [str]
```

### 6.3 `assistant_message_completed` v2 payload

```text
content: str                 # Presenter 渲染后的用户可见 Markdown（短标签 [1]）
tool_calls: [...]            # 与 v1 相同
citations: [{
  ordinal: int,              # 对应 [1]
  evidence_id: str,          # 必须存在于本 Turn TRR
  knowledge_base_id, source_id, source_version_id,
  snapshot_id, locator, locator_schema_version, content_hash
}]
model_supplement: str        # 无补充时为空字符串
```

中间含 Tool Call 的 Assistant 事件 `citations` 为空数组，`model_supplement` 为空。最终用户可见
回答必须在写入本事件前完成 Reference 校验。失败初稿不得成为 Session 事实。

### 6.4 时序

正常知识 Turn：

```text
session_started? → user_message → turn_retrieval_recorded
  → (model_usage_recorded | tool_* | permission_*)*
  → assistant_message_completed → turn_completed
```

`turn_retrieval_recorded` 必须在任何该 Turn 的模型调用用途事件之前。无 Binding 仍写 TRR
（`NOT_CONFIGURED`）。

取消与失败：

| 条件 | 终止事件 |
| --- | --- |
| 知识准备期间协作式取消 | `turn_failed(cancelled, knowledge_preparation_cancelled)` |
| TRR 已写、进程安全中断 | `turn_failed(cancelled, process_interrupted)`；不继续原 Turn |
| Repair 耗尽 | `turn_failed(model_error, invalid_citation)` |
| 其他既有停止 | 沿用 v0.2 StopReason 映射 |

知识准备完成后、模型调用前取消：已写 TRR 则仍须写 `turn_failed` 关闭 Turn。

### 6.5 ContextBuilder v2

```text
ModelRequest = f(RuntimeEvent sequence, current PreparedTurnKnowledge, capabilities)
```

确定性：相同事件、相同 PTK、相同 capabilities → 相同请求。不执行 I/O。PTK Evidence 作为单独
不可信 SystemPart 注入，明确边界文本：文档不是指令。机器 Reference 完整写入该 Part。历史
Assistant 内容保持短标签，不得被解析为当前 Reference。原文中出现的 `K:` 模式必须转义或包裹，
避免与机器 Reference 碰撞。

## 7. Citation 校验与 Repair

合法机器 Reference 正则：`K:<turn_token>:E<positive_integer>`，且 `<turn_token>` 等于本 Turn
PTK，序号对应存在的 Evidence。

非法：未知序号、错误 turn_token、历史标签 `[n]`、被预算裁剪未进入 PTK 的候选、模型捏造 locator。

Repair 合同：

1. 次数 ≤ 1；
2. 同一 PTK、同一 DeepSeek 生成模型；
3. 不检索、不改 Binding、不执行 Tool、不扩大 Evidence；
4. Repair 请求只包含非法说明与原 PTK Evidence 列表；
5. 仍非法则不写 `assistant_message_completed`。

Citation Resolver（展示历史时）：先看 Catalog 生命周期。`DELETE_PENDING`/`DELETED` → 墓碑，禁止
打开原文。对象哈希失败或缺失 → 来源损坏。`INACTIVE`（Deactivate）→ 可打开原文但不再被新检索选中。

## 8. Headless JSON v2

```json
{
  "schema_version": 2,
  "status": "success|error",
  "session_id": "...",
  "turn_id": "...",
  "stop_reason": "...",
  "answer": "...",
  "model_supplement": "...",
  "provider": "...",
  "model": "...",
  "usage": {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0},
  "knowledge": {
    "outcome": "not_configured",
    "bases": [],
    "degradations": []
  },
  "citations": [],
  "error": null
}
```

`knowledge.bases[]` 使用 TRR 的脱敏逐库状态。`citations[]` 与完成事件同一规范事实。v0.3 不输出
schema 1。无知识配置时 outcome 仍出现，值为 `not_configured`。

## 9. Trace 投影增量

在既有安全字段上增加可选知识字段（缺省 null）：`knowledge_outcome`、`operation_id`、
`generation_id`、`revision`、`repair_attempted`、`degradation`。禁止附加 Query/Evidence/Source
正文。

## 10. Composition 与依赖

- 基础安装可 import `jdagent` 并完成无 Binding Turn。
- `charset-normalizer` 与 `pymilvus` 放入 optional extra `rag`。
- 自动解码与 Milvus Adapter 延迟导入；缺 extra 时对应 Use Case 返回稳定配置/依赖错误，不在
  import 时崩溃。
- Fake Index/Embedding 位于默认测试路径，不要求 extra。
