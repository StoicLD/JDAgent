"""Prepared Turn Knowledge values used by retrieval and runtime."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from jdagent.knowledge.types import EmbeddingProfile, RetrievalProfile


class RetrievalOutcome(StrEnum):
    NOT_CONFIGURED = "not_configured"
    UNAVAILABLE = "unavailable"
    PARTIAL = "partial"
    COMPLETE = "complete"
    INSUFFICIENT = "insufficient"


class BaseQueryStatus(StrEnum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class QueryFailureReason(StrEnum):
    BINDING_INVALID = "binding_invalid"
    ACCESS_DENIED = "access_denied"
    PROVIDER_UNAVAILABLE = "provider_unavailable"
    QUERY_FAILED = "query_failed"


def estimate_tokens(text: str) -> int:
    return max(1, (len(text) + 3) // 4)


@dataclass(frozen=True, slots=True)
class FrozenKnowledgeBase:
    binding_id: str
    connection_id: str
    knowledge_base_id: str
    name: str
    generation_id: str | None
    revision: int
    physical_collection: str | None
    embedding_profile: EmbeddingProfile
    retrieval_profile: RetrievalProfile
    language_profile: str


@dataclass(frozen=True, slots=True)
class KnowledgeBaseTurnResult:
    binding_id: str
    connection_id: str
    knowledge_base_id: str
    kb_name: str
    status: BaseQueryStatus
    failure_reason: QueryFailureReason | None
    generation_id: str | None
    revision: int
    physical_collection: str | None
    embedding_profile_fingerprint: str
    retrieval_profile_fingerprint: str
    hit_count: int
    selected_evidence_count: int


@dataclass(frozen=True, slots=True)
class Evidence:
    evidence_id: str
    reference: str
    knowledge_base_id: str
    source_id: str
    source_version_id: str
    snapshot_id: str
    locator: str
    locator_schema_version: int
    content_hash: str
    parent_text: str
    token_estimate: int
    ordinal: int


@dataclass(frozen=True, slots=True)
class EvidenceBudget:
    dense_top_k: int
    bm25_top_k: int
    fused_child_count: int
    rerank_pool_size: int
    parent_count: int
    evidence_tokens: int
    evidence_token_limit: int
    parent_limit: int


@dataclass(frozen=True, slots=True)
class PreparedTurnKnowledge:
    turn_id: str
    turn_token: str
    outcome: RetrievalOutcome
    query_fingerprint: str
    bases: tuple[KnowledgeBaseTurnResult, ...]
    evidence: tuple[Evidence, ...]
    budget: EvidenceBudget
    degradations: tuple[str, ...]
    retrieval_profile_fingerprint: str

    def evidence_by_reference(self, reference: str) -> Evidence | None:
        for item in self.evidence:
            if item.reference == reference:
                return item
        return None


def empty_prepared_knowledge(
    turn_id: str, turn_token: str, query_fingerprint: str
) -> PreparedTurnKnowledge:
    return PreparedTurnKnowledge(
        turn_id=turn_id,
        turn_token=turn_token,
        outcome=RetrievalOutcome.NOT_CONFIGURED,
        query_fingerprint=query_fingerprint,
        bases=(),
        evidence=(),
        budget=EvidenceBudget(20, 20, 0, 40, 0, 0, 6000, 8),
        degradations=(),
        retrieval_profile_fingerprint="",
    )


def outcome_from_counts(
    binding_count: int, success_count: int, failure_count: int, evidence_count: int
) -> RetrievalOutcome:
    if binding_count == 0:
        return RetrievalOutcome.NOT_CONFIGURED
    if success_count == 0:
        return RetrievalOutcome.UNAVAILABLE
    if failure_count > 0:
        return RetrievalOutcome.PARTIAL
    if evidence_count > 0:
        return RetrievalOutcome.COMPLETE
    return RetrievalOutcome.INSUFFICIENT
