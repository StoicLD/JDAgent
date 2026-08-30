"""Offline gold-corpus retrieval and PTK eval."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from jdagent.domain.json import JsonObject, normalize_json
from jdagent.knowledge.catalog import KnowledgeCatalog
from jdagent.knowledge.clock import FakeClock
from jdagent.knowledge.embedding import EmbeddingKind, HashEmbedding
from jdagent.knowledge.index import InMemoryKnowledgeIndex
from jdagent.knowledge.ingestion import KnowledgeIngestion
from jdagent.knowledge.parsing import format_from_path, parse_source
from jdagent.knowledge.preparation import TurnKnowledgePreparation
from jdagent.knowledge.ptk import FrozenKnowledgeBase
from jdagent.knowledge.retrieval import rrf_merge
from jdagent.knowledge.store import ContentAddressedStore
from jdagent.knowledge.types import (
    BindingRecord,
    EmbeddingProfile,
    RetrievalProfile,
)

GOLD_ROOT = Path(__file__).resolve().parents[3] / "testdata" / "v0.3-gold"
DATASET_REVISION = "v0.3-gold-r1"
SOURCE_KB = {
    "zh-leave-policy": "hr",
    "en-expense-handbook": "finance",
    "finance-stipend": "finance",
    "mixed-faq": "mixed",
    "employees": "hr",
    "overlap-a": "hr",
    "overlap-b": "hr",
    "injection": "hr",
}
SKIP_QUERIES = {"Q090", "Q091", "Q092"}


@dataclass(frozen=True, slots=True)
class LayerScore:
    query_id: str
    slice: str
    metric: str
    actual: float
    expected: float
    reason: str


def load_jsonl(path: Path) -> tuple[JsonObject, ...]:
    rows: list[JsonObject] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        loaded = normalize_json(json.loads(line))
        if not isinstance(loaded, dict):
            raise ValueError("JSONL row must be an object")
        rows.append(cast(JsonObject, loaded))
    return tuple(rows)


def parser_locator_failures() -> tuple[LayerScore, ...]:
    queries = {str(row["query_id"]): row for row in load_jsonl(GOLD_ROOT / "queries.jsonl")}
    annotations = {str(row["query_id"]): row for row in load_jsonl(GOLD_ROOT / "annotations.jsonl")}
    locators_by_source: dict[str, set[str]] = {}
    for path in (GOLD_ROOT / "sources").iterdir():
        parsed = parse_source(path.read_bytes(), source_format=format_from_path(path.name))
        locators_by_source[path.stem] = {block.locator for block in parsed.blocks}
    failures: list[LayerScore] = []
    for query_id, annotation in annotations.items():
        if query_id in SKIP_QUERIES:
            continue
        query = queries[query_id]
        gold = annotation.get("gold_locators")
        if not isinstance(gold, list):
            continue
        for item in gold:
            if not isinstance(item, dict):
                continue
            source_key = str(item.get("source_key"))
            locator = str(item.get("locator"))
            required = item.get("required") is True
            if not required:
                continue
            if locator not in locators_by_source.get(source_key, set()):
                failures.append(
                    LayerScore(
                        query_id,
                        str(query.get("slice", "")),
                        "locator_roundtrip",
                        0.0,
                        1.0,
                        "missing_required_locator",
                    )
                )
    return tuple(failures)


async def run_retrieval_eval(
    tmp_path: Path,
) -> tuple[tuple[LayerScore, ...], tuple[LayerScore, ...]]:
    catalog = KnowledgeCatalog(tmp_path / "catalog.sqlite", tmp_path / "backups", clock=FakeClock())
    catalog.open()
    connection = catalog.register_connection(name="local")
    indexes: dict[str, InMemoryKnowledgeIndex] = {}
    kbs: dict[str, str] = {}
    ingestion_by_kb: dict[str, KnowledgeIngestion] = {}
    for name in ("hr", "finance", "mixed"):
        kb = catalog.create_knowledge_base(
            connection_id=connection.connection_id,
            name=name,
            embedding_profile=EmbeddingProfile(dimension=32),
        )
        index = InMemoryKnowledgeIndex()
        indexes[name] = index
        kbs[name] = kb.knowledge_base_id
        ingestion_by_kb[name] = KnowledgeIngestion(
            catalog,
            ContentAddressedStore(tmp_path / "objects"),
            index,
        )
    source_ids: dict[str, str] = {}
    for path in sorted((GOLD_ROOT / "sources").iterdir()):
        key = path.stem
        kb_name = SOURCE_KB[key]
        result = await ingestion_by_kb[kb_name].add_file(kbs[kb_name], path)
        source_ids[key] = result.source_id
    queries = load_jsonl(GOLD_ROOT / "queries.jsonl")
    annotations = {str(row["query_id"]): row for row in load_jsonl(GOLD_ROOT / "annotations.jsonl")}
    child_scores: list[LayerScore] = []
    ptk_scores: list[LayerScore] = []
    preparation = TurnKnowledgePreparation(indexes=indexes)
    now = catalog.get_knowledge_base(kbs["hr"]).created_at
    for query in queries:
        query_id = str(query["query_id"])
        if query_id in SKIP_QUERIES:
            continue
        annotation = annotations[query_id]
        kb_names = [str(name) for name in cast(list[object], query["knowledge_bases"])]
        bindings = tuple(
            BindingRecord(f"bind_{name}", "ws", connection.connection_id, name, now)
            for name in kb_names
        )
        frozen = tuple(
            (
                FrozenKnowledgeBase(
                    f"bind_{name}",
                    connection.connection_id,
                    name,
                    name,
                    catalog.get_knowledge_base(kbs[name]).current_generation_id,
                    catalog.get_knowledge_base(kbs[name]).current_revision,
                    catalog.get_knowledge_base(kbs[name]).current_generation_id,
                    EmbeddingProfile(dimension=32),
                    RetrievalProfile(),
                    "mixed_zh_en_v1",
                ),
                None,
            )
            for name in kb_names
        )
        ptk = await preparation.prepare(
            turn_id=query_id,
            query_text=str(query["text"]),
            bindings=bindings,
            frozen=frozen,
            turn_token=query_id.lower(),
        )
        required = _required_locators(annotation)
        child_hits: set[str] = set()
        dense_hits: set[str] = set()
        hybrid_hits: set[str] = set()
        query_text = str(query["text"])
        query_vector = (
            await HashEmbedding().embed(
                (query_text,),
                EmbeddingProfile(dimension=32),
                input_kind=EmbeddingKind.QUERY,
            )
        ).vectors[0]
        for base in frozen:
            frozen_base = base[0]
            if frozen_base.generation_id is None:
                continue
            index = indexes[frozen_base.knowledge_base_id]
            bm25 = index.search_bm25(
                frozen_base.generation_id,
                frozen_base.revision,
                query_text,
                10,
            )
            dense = index.search_dense(
                frozen_base.generation_id,
                frozen_base.revision,
                query_vector,
                10,
            )
            hybrid = rrf_merge((dense, bm25), k=60, limit=10)
            child_hits.update(hit.locator for hit in bm25)
            dense_hits.update(hit.locator for hit in dense)
            hybrid_hits.update(hit.locator for hit in hybrid)
        child_recall = _recall(required, child_hits)
        dense_recall = _recall(required, dense_hits)
        hybrid_recall = _recall(required, hybrid_hits)
        ptk_locators = {item.locator for item in ptk.evidence}
        ptk_coverage = _recall(required, ptk_locators)
        slice_name = str(query.get("slice", ""))
        child_scores.append(
            LayerScore(
                query_id,
                slice_name,
                "recall@10",
                child_recall,
                1.0,
                "missing_required_locator" if child_recall < 1.0 else "ok",
            )
        )
        child_scores.append(
            LayerScore(
                query_id,
                slice_name,
                "dense_recall@10",
                dense_recall,
                1.0,
                "missing_required_locator" if dense_recall < 1.0 else "ok",
            )
        )
        child_scores.append(
            LayerScore(
                query_id,
                slice_name,
                "hybrid_recall@10",
                hybrid_recall,
                1.0,
                "missing_required_locator" if hybrid_recall < 1.0 else "ok",
            )
        )
        ptk_scores.append(
            LayerScore(
                query_id,
                slice_name,
                "ptk_coverage",
                ptk_coverage,
                1.0,
                "missing_required_locator" if ptk_coverage < 1.0 else "ok",
            )
        )
    catalog.close()
    return tuple(child_scores), tuple(ptk_scores)


def fake_hybrid_drop(child: tuple[LayerScore, ...]) -> float:
    """Return best-single minus hybrid mean recall. Live embedding owns the ≤0.02 gate."""

    bm25 = [item.actual for item in child if item.metric == "recall@10"]
    dense = [item.actual for item in child if item.metric == "dense_recall@10"]
    hybrid = [item.actual for item in child if item.metric == "hybrid_recall@10"]
    if not bm25 or not dense or not hybrid:
        return 0.0
    best = max(sum(bm25) / len(bm25), sum(dense) / len(dense))
    return best - (sum(hybrid) / len(hybrid))


def write_failures(path: Path, layer: str, scores: tuple[LayerScore, ...]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as stream:
        for score in scores:
            payload = {
                "dataset_revision": DATASET_REVISION,
                "layer": layer,
                "query_id": score.query_id,
                "slice": score.slice,
                "mode": "hybrid_rrf",
                "metric": score.metric,
                "expected": score.expected,
                "actual": score.actual,
                "run_index": 0,
                "config_fingerprint": "hash-embedding-offline",
                "safe_reason": score.reason,
            }
            stream.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")


def _required_locators(annotation: JsonObject) -> set[str]:
    gold = annotation.get("gold_locators")
    locators: set[str] = set()
    if not isinstance(gold, list):
        return locators
    for item in gold:
        if isinstance(item, dict) and item.get("required") is True:
            locators.add(str(item.get("locator")))
    return locators


def _recall(required: set[str], actual: set[str]) -> float:
    if not required:
        return 1.0
    matched = sum(1 for gold in required if any(locator_covers(gold, item) for item in actual))
    return matched / len(required)


def locator_covers(gold: str, actual: str) -> bool:
    if gold == actual:
        return True
    if gold.startswith("csv:"):
        return gold.rsplit(":", 1)[0] == actual.rsplit(":", 1)[0]
    if "/r" in gold and gold.rsplit("c", 1)[0] != gold:
        row = gold.rsplit("c", 1)[0]
        return actual.startswith(row)
    return False
