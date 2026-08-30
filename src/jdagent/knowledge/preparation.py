"""Turn Knowledge Preparation: freeze bindings, retrieve, fuse, expand, budget."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from typing import Protocol
from uuid import uuid4

from jdagent.knowledge.catalog import KnowledgeCatalog
from jdagent.knowledge.embedding import EmbeddingKind, EmbeddingPort, HashEmbedding
from jdagent.knowledge.errors import KnowledgeError, KnowledgeErrorCode
from jdagent.knowledge.index import IndexHit, KnowledgeIndex
from jdagent.knowledge.ptk import (
    BaseQueryStatus,
    Evidence,
    EvidenceBudget,
    FrozenKnowledgeBase,
    KnowledgeBaseTurnResult,
    PreparedTurnKnowledge,
    QueryFailureReason,
    empty_prepared_knowledge,
    estimate_tokens,
    outcome_from_counts,
)
from jdagent.knowledge.reranker import RerankerPort
from jdagent.knowledge.retrieval import KnowledgeProvider, rrf_merge
from jdagent.knowledge.types import BindingRecord, RetrievalProfile


class FrozenBaseResolver(Protocol):
    def resolve(
        self, bindings: tuple[BindingRecord, ...]
    ) -> tuple[tuple[FrozenKnowledgeBase | None, QueryFailureReason | None], ...]: ...


class CatalogBaseResolver:
    """Resolve live catalog rows into frozen search targets."""

    def __init__(self, catalog: KnowledgeCatalog) -> None:
        self._catalog = catalog

    def resolve(
        self, bindings: tuple[BindingRecord, ...]
    ) -> tuple[tuple[FrozenKnowledgeBase | None, QueryFailureReason | None], ...]:
        resolved: list[tuple[FrozenKnowledgeBase | None, QueryFailureReason | None]] = []
        for binding in bindings:
            try:
                kb = self._catalog.get_knowledge_base(binding.knowledge_base_id)
            except KnowledgeError as error:
                reason = (
                    QueryFailureReason.ACCESS_DENIED
                    if error.code is KnowledgeErrorCode.ACCESS_DENIED
                    else QueryFailureReason.BINDING_INVALID
                )
                resolved.append((None, reason))
                continue
            generation_id = kb.current_generation_id
            resolved.append(
                (
                    FrozenKnowledgeBase(
                        binding.binding_id,
                        binding.connection_id,
                        kb.knowledge_base_id,
                        kb.name,
                        generation_id,
                        kb.current_revision,
                        generation_id,
                        kb.embedding_profile,
                        kb.retrieval_profile,
                        kb.language_profile,
                    ),
                    None,
                )
            )
        return tuple(resolved)


class TurnKnowledgePreparation:
    """Hide per-KB retrieval, fusion, expansion, and outcome aggregation."""

    def __init__(
        self,
        *,
        embedding: EmbeddingPort | None = None,
        indexes: Mapping[str, KnowledgeIndex] | None = None,
        reranker: RerankerPort | None = None,
        provider: KnowledgeProvider | None = None,
    ) -> None:
        self._embedding = embedding or HashEmbedding()
        self._indexes = dict(indexes or {})
        self._reranker = reranker
        self._provider = provider or KnowledgeProvider()

    async def prepare(
        self,
        *,
        turn_id: str,
        query_text: str,
        bindings: tuple[BindingRecord, ...],
        frozen: tuple[tuple[FrozenKnowledgeBase | None, QueryFailureReason | None], ...],
        turn_token: str | None = None,
    ) -> PreparedTurnKnowledge:
        token = turn_token or uuid4().hex[:8]
        digest = hashlib.sha256(query_text.encode("utf-8")).hexdigest()
        if not bindings:
            return empty_prepared_knowledge(turn_id, token, digest)

        groups: dict[str, list[int]] = {}
        for index, (base, failure) in enumerate(frozen):
            if base is None or failure is not None:
                continue
            groups.setdefault(base.embedding_profile.fingerprint(), []).append(index)

        vectors: dict[str, tuple[float, ...]] = {}
        for fingerprint, members in groups.items():
            profile = frozen[members[0]][0]
            if profile is None or profile.retrieval_profile.mode == "bm25":
                continue
            embedded = await self._embedding.embed(
                (query_text,),
                profile.embedding_profile,
                input_kind=EmbeddingKind.QUERY,
            )
            vectors[fingerprint] = embedded.vectors[0]

        base_results: list[KnowledgeBaseTurnResult] = []
        kb_rankings: list[tuple[IndexHit, ...]] = []
        success = 0
        failure_count = 0
        default_profile = RetrievalProfile()
        for binding, (base, failure) in zip(bindings, frozen, strict=True):
            if base is None or failure is not None:
                failure_count += 1
                base_results.append(
                    KnowledgeBaseTurnResult(
                        binding.binding_id,
                        binding.connection_id,
                        binding.knowledge_base_id,
                        binding.knowledge_base_id,
                        BaseQueryStatus.FAILED,
                        failure or QueryFailureReason.BINDING_INVALID,
                        None,
                        0,
                        None,
                        "",
                        "",
                        0,
                        0,
                    )
                )
                continue
            default_profile = base.retrieval_profile
            index = self._indexes.get(base.knowledge_base_id)
            if index is None:
                failure_count += 1
                base_results.append(
                    _failed_base(binding, base, QueryFailureReason.PROVIDER_UNAVAILABLE)
                )
                continue
            queried = self._provider.search(
                index,
                base,
                query_text,
                vectors.get(base.embedding_profile.fingerprint()),
                base.retrieval_profile,
            )
            if queried.failure_reason is not None:
                failure_count += 1
                base_results.append(_failed_base(binding, base, queried.failure_reason))
                continue
            success += 1
            kb_rankings.append(queried.hits)
            fingerprint = base.retrieval_profile.fingerprint(
                base.embedding_profile.fingerprint(),
                base.language_profile,
            )
            base_results.append(
                KnowledgeBaseTurnResult(
                    binding.binding_id,
                    binding.connection_id,
                    base.knowledge_base_id,
                    base.name,
                    BaseQueryStatus.SUCCEEDED,
                    None,
                    base.generation_id,
                    base.revision,
                    base.physical_collection,
                    base.embedding_profile.fingerprint(),
                    fingerprint,
                    queried.hit_count,
                    0,
                )
            )

        fused = rrf_merge(
            tuple(kb_rankings),
            k=default_profile.rrf_k,
            limit=default_profile.rerank_pool_size,
        )
        degradations: list[str] = []
        ranked = fused
        if self._reranker is not None and fused:
            try:
                ranked = self._reranker.rerank(query_text, fused)
            except Exception:
                ranked = fused
                degradations.append("reranker_fallback")

        evidence = _expand_parents(ranked, token, default_profile)
        selected_by_kb: dict[str, int] = {}
        for item in evidence:
            selected_by_kb[item.knowledge_base_id] = (
                selected_by_kb.get(item.knowledge_base_id, 0) + 1
            )
        finalized = tuple(
            KnowledgeBaseTurnResult(
                result.binding_id,
                result.connection_id,
                result.knowledge_base_id,
                result.kb_name,
                result.status,
                result.failure_reason,
                result.generation_id,
                result.revision,
                result.physical_collection,
                result.embedding_profile_fingerprint,
                result.retrieval_profile_fingerprint,
                result.hit_count,
                selected_by_kb.get(result.knowledge_base_id, 0),
            )
            for result in base_results
        )
        outcome = outcome_from_counts(len(bindings), success, failure_count, len(evidence))
        profile_fp = default_profile.fingerprint("", "mixed_zh_en_v1")
        return PreparedTurnKnowledge(
            turn_id=turn_id,
            turn_token=token,
            outcome=outcome,
            query_fingerprint=digest,
            bases=finalized,
            evidence=evidence,
            budget=EvidenceBudget(
                default_profile.dense_top_k,
                default_profile.bm25_top_k,
                len(fused),
                default_profile.rerank_pool_size,
                len(evidence),
                sum(item.token_estimate for item in evidence),
                default_profile.evidence_token_limit,
                default_profile.parent_limit,
            ),
            degradations=tuple(degradations),
            retrieval_profile_fingerprint=profile_fp,
        )


def _failed_base(
    binding: BindingRecord,
    base: FrozenKnowledgeBase,
    reason: QueryFailureReason,
) -> KnowledgeBaseTurnResult:
    return KnowledgeBaseTurnResult(
        binding.binding_id,
        binding.connection_id,
        base.knowledge_base_id,
        base.name,
        BaseQueryStatus.FAILED,
        reason,
        base.generation_id,
        base.revision,
        base.physical_collection,
        base.embedding_profile.fingerprint(),
        base.retrieval_profile.fingerprint(
            base.embedding_profile.fingerprint(),
            base.language_profile,
        ),
        0,
        0,
    )


def _expand_parents(
    hits: tuple[IndexHit, ...],
    turn_token: str,
    profile: RetrievalProfile,
) -> tuple[Evidence, ...]:
    selected: list[Evidence] = []
    seen_parents: set[tuple[str, str]] = set()
    tokens_used = 0
    ordinal = 1
    for hit in hits:
        key = (hit.source_version_id, hit.parent_id or hit.locator)
        if key in seen_parents:
            continue
        parent_text = hit.parent_text or hit.text
        parent_tokens = estimate_tokens(parent_text)
        if parent_tokens > profile.parent_token_limit:
            continue
        if tokens_used + parent_tokens > profile.evidence_token_limit:
            continue
        if len(selected) >= profile.parent_limit:
            break
        seen_parents.add(key)
        tokens_used += parent_tokens
        selected.append(
            Evidence(
                evidence_id=f"ev_{ordinal}",
                reference=f"K:{turn_token}:E{ordinal}",
                knowledge_base_id=hit.knowledge_base_id,
                source_id=hit.source_id,
                source_version_id=hit.source_version_id,
                snapshot_id=hit.snapshot_id,
                locator=hit.locator,
                locator_schema_version=hit.locator_schema_version,
                content_hash=hit.content_hash,
                parent_text=parent_text,
                token_estimate=parent_tokens,
                ordinal=ordinal,
            )
        )
        ordinal += 1
    return tuple(selected)
