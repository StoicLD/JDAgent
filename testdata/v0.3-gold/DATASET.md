# v0.3 Gold Corpus 与 Eval 协议

状态：`Proposed`

日期：2026-08-30

Dataset revision：`v0.3-gold-r1`

关联需求：[v0.3 需求 AC-049–054](../../docs/requirements/v0.3-knowledge-retrieval.md)

关联架构：[高层设计 §17](../../docs/architecture/v0.3-knowledge-retrieval.md)

本目录是 v0.3 规范 Gold Corpus。公开 IR 数据集不得替代本语料的生命周期与 Citation 场景。

## 1. 目录

```text
testdata/v0.3-gold/
  DATASET.md              # 本文件：Schema、协议、门禁
  BASELINE.md             # 无 RAG 实现前基线
  manifest.json           # 文件清单与内容哈希（实现后由 Eval runner 校验）
  sources/                # 规范 UTF-8 正文（编码变体见 encodings/）
  encodings/              # BOM / UTF-16 / GB18030 / 损坏样本
  queries.jsonl           # 检索与回答查询
  annotations.jsonl       # Locator 与 claim–evidence 标注
  failures/               # Eval 失败样本输出目录（gitignore 内容，保留 .gitkeep）
```

`failures/` 只保存运行产物模板；实际失败 JSONL 不得提交用户数据。仓库仅保留 `.gitkeep` 与
格式说明。

## 2. Corpus / Query / Annotation Schema

### 2.1 Source 清单（manifest `sources[]`）

```text
source_key: str            # 稳定键，如 zh-leave-policy
path: str                  # 相对本目录
format: txt|markdown|csv
language: zh|en|mixed|csv
knowledge_base_key: str    # 逻辑库：hr | finance | mixed
sha256: str                # 规范 UTF-8 文件哈希（encodings 变体另计）
```

### 2.2 `queries.jsonl` 每行

```json
{
  "query_id": "Q001",
  "text": "...",
  "language": "zh",
  "knowledge_bases": ["hr"],
  "answerable": true,
  "slice": "zh",
  "scenario": "single_fact"
}
```

`slice`：`zh` | `en` | `csv` | `mixed` | `multikb` | `negative` | `injection` | `lifecycle`。
`scenario` 另见第 4 节。

### 2.3 `annotations.jsonl` 每行

```json
{
  "query_id": "Q001",
  "gold_locators": [
    {
      "source_key": "zh-leave-policy",
      "locator": "md:h2[0]/p[0]",
      "required": true
    }
  ],
  "claims": [
    {
      "claim_id": "Q001-C1",
      "text": "年假为15天",
      "must_cite": true,
      "evidence_source_key": "zh-leave-policy",
      "evidence_locator": "md:h2[0]/p[0]"
    }
  ],
  "unanswerable": false,
  "notes": ""
}
```

Locator 字符串由 Parser 合同生成；本 revision 的 Gold Locator 在实现 Parser 后用往返测试冻结。
M0 写入的 locator 是设计意图；M2 关闭时必须用确定性输出回填本文件，并记录
`locator_schema_version=1`。若实现 locator 语法与草稿不同，以测试冻结值为唯一事实并更新本语料，
不保留第二套。

### 2.4 失败样本格式（`failures/*.jsonl`）

```json
{
  "dataset_revision": "v0.3-gold-r1",
  "layer": "child_retrieval|final_ptk|answer_citation|parser",
  "query_id": "Q001",
  "slice": "zh",
  "mode": "hybrid_rrf",
  "metric": "recall@10",
  "expected": 1.0,
  "actual": 0.0,
  "run_index": 0,
  "config_fingerprint": "...",
  "safe_reason": "missing_required_locator"
}
```

禁止写入 Query 全文、Evidence 全文、模型回答正文或秘密。允许 `query_id` 与 locator、ID、指纹。

## 3. 分层执行与随机性协议

| 层 | 随机性 | 判定 |
| --- | --- | --- |
| Parser/Locator/Revision/墓碑 | 无 | 单次 100% |
| Child Retrieval（Recall@10, nDCG@10） | 无（Fake）或有（真实 Embedding） | Fake 单次；Live Embedding 单次记录 fingerprint |
| Final PTK Gold Support Coverage | 同上 | 单次；多证据题另报“全部 required locator 均保留”比例 |
| Answer/Citation | 有（DeepSeek） | 每题独立 3 次；temperature 冻结 |

DeepSeek 聚合（正式、不可挑选最佳一次）：

- 冻结：model id、prompt revision、temperature=0、timeout、其它 settings、dataset revision。
- 每次运行保存 `run_index` 0..2 的 Citation Precision/Completeness/无答案错误支持率。
- **发布判定使用三次运行的中位数**；任一层中位数未过门禁即失败。
- 三次中若有协议失败（非法 JSON、超时），该次记为该指标 0 或 Completeness 0，仍计入中位数，
  不得丢弃后重跑直到满意。
- 允许额外调试运行，但不得替换这三次正式结果。

无答案错误知识支持率：`unanswerable=true` 的题目中，模型仍对知识主张给出非墓碑 Citation 的比例。

## 4. 本 revision 覆盖

| 场景 | 来源 | 查询 |
| --- | --- | --- |
| 中文政策 | `sources/zh-leave-policy.md` | Q001–Q003 |
| 英文手册 | `sources/en-expense-handbook.txt` | Q010–Q012 |
| 中英混合 FAQ | `sources/mixed-faq.md` | Q020–Q021 |
| CSV | `sources/employees.csv` | Q030–Q032 |
| 相似重复 | `sources/overlap-a.txt` `overlap-b.txt` | Q040 |
| 无答案 | （现有库） | Q050 |
| 冲突 | 两库不同数字 | Q060 |
| 注入 | `sources/injection.md` | Q070 |
| 多库 | hr+finance | Q080 |
| 编码 | `encodings/` | 由 Parser 测试引用，不走检索题 |

生命周期（Replace/Deactivate/Delete/墓碑）与故障（Catalog/Index/Embedding/Repair）用夹具构造，
不把可变状态提交进 sources/。查询 Q090–Q099 保留为 lifecycle 标注模板，由测试注入。

## 5. 门禁（与架构 §17.4 一致）

- Locator 往返、Citation 目标合法率、Revision 隔离、墓碑、失败分类：100%
- Child Recall@10 总体 ≥ 0.85；zh/en/csv ≥ 0.75
- Final PTK Gold Support Coverage 总体 ≥ 0.85；切片 ≥ 0.75
- Hybrid 相对最佳单路 Recall@10 下降 ≤ 0.02
- Citation Precision ≥ 0.90；Completeness ≥ 0.85；无答案错误支持率 ≤ 0.05
- Cross-Encoder 增益门禁仅在真实 Adapter 候选启用时适用；默认关闭故不阻塞 v0.3

Fake Embedding 的 Recall 门禁用于离线回归：向量由规范化文本确定性展开，语义近似靠词项重叠的
BM25 与哈希碰撞的有限 dense 信号。若 Fake 下 Hybrid 门禁与真实 Embedding 行为冲突，以 Fake 保
证契约，真实 Recall 记入 live 报告，不得为过 Fake 而改 Gold 标注。

## 6. 配置指纹（每次 Eval 必须保存）

`dataset_revision`、Embedding Profile、Retrieval Profile、DeepSeek model、prompt revision、
temperature/settings、language profile、代码 revision、开始/结束时间、Usage（Live）。
