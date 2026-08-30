# v0.3 Milvus Collection、Revision Filter 与 Saga 合同

状态：`Proposed`

日期：2026-08-30

关联架构：[高层设计 §7–§9、§8](v0.3-knowledge-retrieval.md)

关联合同：[知识 Port 与 Runtime Event v2](knowledge-contracts-v0.3.md)

关联决策：[ADR-0006](../decisions/ADR-0006-milvus-standalone-knowledge-index.md)

本文固定 Index Adapter 内部物理形状与跨存储收敛，不把 Milvus 类型泄漏到 Agent Core。

## 1. Index Generation 与物理 Collection

新 Generation 的兼容性输入（任一变化即新代）：Parser/Locator Schema、Chunking、Embedding
Profile、Dense/BM25 字段、Retrieval Language Profile/词典、Distance Metric、HNSW 关键参数、
Contextual Retrieval 字段合同（v0.3 默认关闭该字段）。

命名（Adapter 私有，可调整只要 Catalog 映射唯一）：

```text
jdagent_kb_{kb_id_compact}_g_{generation_compact}
```

Catalog `index_generations.physical_collection` 是 Runtime Search 的唯一目标。Milvus alias 若使用，
仅作为运维镜像；Reconciler 只能把 alias **单向**调到 Catalog 记录的 Generation，禁止用 alias 写回
Catalog。Catalog 指向的 Collection 缺失 → `PROVIDER_UNAVAILABLE`。

### 1.1 字段（逻辑）

| 字段 | 用途 |
| --- | --- |
| `chunk_id` | 主键；稳定，来自 snapshot+locator |
| `source_id` / `source_version_id` / `snapshot_id` | 身份 |
| `parent_id` | Parent Segment 身份 |
| `locator` / `locator_schema_version` | 回指原文 |
| `content_hash` | Child 文本哈希 |
| `dense_vector` | FLOAT_VECTOR，维度=Embedding Profile |
| `bm25_text` | 送入 Analyzer 的检索文本；Citation 不用此字段若与原文分离 |
| `valid_from_revision` | INT64 |
| `valid_to_revision` | INT64；0 表示正无穷（NULL 在部分标量过滤中不便，Adapter 必须在两边一致翻译） |

v0.3 选择：`valid_to_revision=0` 表示仍有效。Filter 必须与写入约定一致，并有 Fake 对等测试。

Dense：HNSW，度量与 Profile 一致（默认 COSINE，若 Embedding 已归一化）。BM25：Milvus
Analyzer + Function 路径；Index 与 Query 使用同一 `mixed_zh_en_v1` 配置。离线 Fake 用空白分词
+ 拉丁/数字保留近似该 Profile，不声称通过 Analyzer 门禁。

### 1.2 `mixed_zh_en_v1` 起点

- 中文：Milvus Chinese/Jieba tokenizer（真实 Adapter）；
- Filter：保留 Latin 与数字 token，去掉无意义标点；
- 领域词典为空；词典变更 → 新 Language Profile 版本 + 新 Generation；
- 真实 Analyzer fixture 使用 `run_analyzer` 固定：中文词、英文标识符、数字、Markdown 标题、CSV
  值。缺 Milvus 时该 fixture 标为 live skip，不降低离线门禁。

## 2. Content Revision Filter

冻结 revision 为整数 `R`。每条 Dense 与 BM25 请求的过滤表达式等价于：

```text
valid_from_revision <= R AND (valid_to_revision == 0 OR valid_to_revision > R)
```

必须在 top-K 前由引擎执行。构建 Revision N：

1. 新可见 chunk：`valid_from_revision=N`，`valid_to_revision=0`；
2. 被替换/停用/删除的旧 chunk：写入或 upsert `valid_to_revision=N`（仍对 N-1 可见）；
3. 全部写入与校验完成后，Catalog 短事务把 `current_revision` 设为 N。

Turn 开始时读取 Catalog 的 current generation_id + revision + physical_collection 并冻结。检索不
持有 Mutation Lease。

故障测试（必须离线 Fake 覆盖，Live 复验）：

- Catalog 与 alias 指向不同 Generation：Search 仍打 Catalog Collection；
- 物理 Collection 缺失：`PROVIDER_UNAVAILABLE`；
- Dense 与 BM25 混入多个 Revision 的数据：过滤后只见冻结 R；
- Reconciler 在 alias 切换前后崩溃：收敛后 alias 等于 Catalog 或 alias 被忽略。

## 3. Knowledge Mutation Lease

用户级 Catalog 全局同一时刻一个活跃 Mutation/Reconcile：

| 参数 | P1 起始值 |
| --- | --- |
| TTL | 5 分钟 |
| Heartbeat | 30 秒 |
| 冲突 | `KNOWLEDGE_BUSY`，不排队 |
| 重试 | 同一 `operation_id` 恢复原 Saga，不新建 Source Version |
| 过期 | 仅 Reconciler 或显式 `retry` 接管 |
| 激活 | 短 Catalog 事务 |
| Retrieval | 不取 Lease |

Lease 字段：`operation_id`、`owner`（进程 token）、`acquired_at`、`heartbeat_at`、`expires_at`。
测试注入 Fake Clock，禁止用随机 sleep 证明 TTL。

长时间 Embedding/Milvus I/O 不持有 OS 文件锁；只以 Lease 行阻止其他 Writer。

## 4. Ingest Saga（每文件）

```text
RECEIVED → RAW_STORED → PARSED → INDEXING → INDEX_VALIDATED → ACTIVE
```

规则：

- 稳定 `operation_id`；文件级 all-or-nothing；多文件批次允许部分成功并返回逐文件错误。
- `RECEIVED`：校验大小、批次 ≤100 个文件、磁盘预留；尚未引用新对象。
- `RAW_STORED`：对象在 Store 中且哈希匹配；Catalog 记录 raw_hash。
- `PARSED`：解码+Parser+Chunk；Snapshot 对象已存储；失败不激活，保留 raw 对象。
- `INDEXING`：Embedding + Index upsert（新 revision 不可见）。
- `INDEX_VALIDATED`：抽样检索或计数校验写入；失败不激活。
- `ACTIVE`：Catalog 切换 current revision/generation；此后新 Turn 可见。

Replace：对新内容走完整 Saga，最后一步切换激活指针；失败则旧 Source Version 仍为检索目标。

崩溃：任一阶段前后退出，Reconcile 根据持久 `saga_stage` 决定 resume、abort-keep-old 或完成激活。
不得把半可见 Revision 暴露给 Retrieval。

## 5. Deactivate / Reactivate / Delete

### 5.1 Deactivate / Reactivate

Deactivate：构建排除该 Source 的新 Revision 并激活；生命周期 `INACTIVE`；原文仍可经 Citation
打开；不墓碑。Reactivate：新 Revision 重新包含其当前 Source Version。

### 5.2 Delete

```text
DELETE_REVISION_BUILT
  → 短事务：激活排除 Revision AND sources.lifecycle=DELETE_PENDING
  → 墓碑从此刻对新 Resolver 生效
  → 异步删除无引用对象与索引投影
  → 全部确认后 lifecycle=DELETED
```

激活前失败：旧 Revision 与可打开 Citation 保持；不得提前墓碑。

`DELETED` 后 Catalog 保留最小身份与 Locator，供历史 Citation 显示墓碑。共享对象仅零引用后 GC。

对象意外缺失：`SOURCE_CORRUPT`，禁止改写为 `DELETE_PENDING`/`DELETED`。

## 6. Reconcile 与 GC

Reconcile：

1. 打开 Catalog，损坏则立即停止（禁止 GC）；
2. 过期 Lease 的未完成 operation 按 stage 接管；
3. alias（若存在）单向对齐 Catalog；
4. 不修改其他 Writer 未过期 Lease。

GC：

- 旧 Generation 默认保留 7 天后，且无活跃 operation 依赖，才 drop Collection 与不可达对象；
- Source Delete 不受这 7 天保护（逻辑删除已生效，物理尽快收敛）；
- 自动 GC 只删除 Catalog 明确不可达且满足策略的对象。

## 7. 错误码（知识模块）

| 码 | 含义 |
| --- | --- |
| `CATALOG_CORRUPT` | 轻量或完整完整性失败 |
| `KNOWLEDGE_BUSY` | Lease 未过期 |
| `SOURCE_TOO_LARGE` / `BATCH_TOO_LARGE` / `CSV_FIELD_TOO_LARGE` | 限额 |
| `ENCODING_AMBIGUOUS` / `ENCODING_FAILED` | 解码 |
| `PARSE_FAILED` | 格式 |
| `DEPENDENCY_MISSING` | rag extra 或 Milvus/Embedding 配置缺失 |
| `PROVIDER_UNAVAILABLE` | 已解析但依赖不可用 |
| `SOURCE_CORRUPT` | 对象缺失或哈希失败 |
| `CONFIRMATION_REQUIRED` | Headless 缺少 `--yes` |

Adapter 异常文本可作诊断，须脱敏，不能替代上表或四值检索失败原因。
