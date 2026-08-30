"""Per-knowledge-base retrieval and rank fusion."""

from __future__ import annotations

from dataclasses import dataclass

from jdagent.knowledge.errors import KnowledgeError, KnowledgeErrorCode
from jdagent.knowledge.index import IndexHit, KnowledgeIndex
from jdagent.knowledge.ptk import FrozenKnowledgeBase, QueryFailureReason
from jdagent.knowledge.types import RetrievalProfile


@dataclass(frozen=True, slots=True)
class KnowledgeQueryResult:
    hits: tuple[IndexHit, ...]
    hit_count: int
    failure_reason: QueryFailureReason | None = None


def rrf_merge(
    rankings: tuple[tuple[IndexHit, ...], ...],
    *,
    k: int,
    limit: int,
) -> tuple[IndexHit, ...]:
    scores: dict[tuple[str, str], float] = {}
    first: dict[tuple[str, str], IndexHit] = {}
    for ranking in rankings:
        for rank, hit in enumerate(ranking, start=1):
            key = (hit.source_version_id, hit.locator)
            scores[key] = scores.get(key, 0.0) + 1.0 / (k + rank)
            first.setdefault(key, hit)
    ordered = sorted(scores.items(), key=lambda item: item[1], reverse=True)
    merged: list[IndexHit] = []
    for key, score in ordered[:limit]:
        hit = first[key]
        merged.append(
            IndexHit(
                hit.chunk_id,
                hit.source_id,
                hit.source_version_id,
                hit.snapshot_id,
                hit.parent_id,
                hit.locator,
                hit.content_hash,
                hit.text,
                score,
                hit.parent_text,
                hit.locator_schema_version,
                hit.knowledge_base_id,
            )
        )
    return tuple(merged)


class KnowledgeProvider:
    """Retrieve one frozen knowledge base without embedding or cross-KB fusion."""

    def search(
        self,
        index: KnowledgeIndex,
        frozen: FrozenKnowledgeBase,
        query_text: str,
        query_vector: tuple[float, ...] | None,
        profile: RetrievalProfile,
    ) -> KnowledgeQueryResult:
        generation_id = frozen.generation_id or frozen.physical_collection
        if generation_id is None:
            return KnowledgeQueryResult((), 0, QueryFailureReason.PROVIDER_UNAVAILABLE)
        try:
            dense: tuple[IndexHit, ...] = ()
            bm25: tuple[IndexHit, ...] = ()
            mode = profile.mode
            if mode in {"dense", "hybrid_rrf"}:
                if query_vector is None:
                    return KnowledgeQueryResult((), 0, QueryFailureReason.QUERY_FAILED)
                dense = index.search_dense(
                    generation_id,
                    frozen.revision,
                    query_vector,
                    profile.dense_top_k,
                )
            if mode in {"bm25", "hybrid_rrf"}:
                bm25 = index.search_bm25(
                    generation_id,
                    frozen.revision,
                    query_text,
                    profile.bm25_top_k,
                )
            if mode == "dense":
                hits = dense[: profile.fused_child_limit]
            elif mode == "bm25":
                hits = bm25[: profile.fused_child_limit]
            else:
                hits = rrf_merge((dense, bm25), k=profile.rrf_k, limit=profile.fused_child_limit)
            return KnowledgeQueryResult(hits, len(hits))
        except KnowledgeError as error:
            reason = (
                QueryFailureReason.PROVIDER_UNAVAILABLE
                if error.code is KnowledgeErrorCode.PROVIDER_UNAVAILABLE
                else QueryFailureReason.QUERY_FAILED
            )
            return KnowledgeQueryResult((), 0, reason)
