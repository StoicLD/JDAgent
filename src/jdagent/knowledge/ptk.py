"""定义本轮检索得到的证据、引用标记和检索结果。"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from jdagent.knowledge.types import EmbeddingProfile, RetrievalProfile


class RetrievalOutcome(StrEnum):
    """描述整轮检索的总体状态，区分未配置、不可用、部分成功和证据不足。"""

    NOT_CONFIGURED = "not_configured"
    UNAVAILABLE = "unavailable"
    PARTIAL = "partial"
    COMPLETE = "complete"
    INSUFFICIENT = "insufficient"


class BaseQueryStatus(StrEnum):
    """记录一次检索中某个知识库是否成功完成查询。"""

    SUCCEEDED = "succeeded"
    FAILED = "failed"


class QueryFailureReason(StrEnum):
    """区分绑定无效、权限不足、后端不可用与查询失败，供界面说明降级原因。"""

    BINDING_INVALID = "binding_invalid"
    ACCESS_DENIED = "access_denied"
    PROVIDER_UNAVAILABLE = "provider_unavailable"
    QUERY_FAILED = "query_failed"


def estimate_tokens(text: str) -> int:
    return max(1, (len(text) + 3) // 4)


@dataclass(frozen=True, slots=True)
class FrozenKnowledgeBase:
    """本轮固定的知识库读取目标，包含物理集合、修订号及向量和检索配置。"""

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
    """保存单库本轮实际使用的版本、命中数、入选证据数以及失败原因。"""

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
    """本轮可引用的一段父段原文，将临时引用标识与可长期核验的来源版本和位置关联。"""

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
    kb_name: str = ""
    source_name: str = ""


@dataclass(frozen=True, slots=True)
class EvidenceBudget:
    """记录候选数、父段数与证据 Token 用量，解释哪些限制影响了最终可见的材料。"""

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
    """一轮检索完成后的固定结果，汇集各库状态、入选证据、预算和降级信息，供模型请求与引用校验共用。"""

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
    turn_id: str,
    turn_token: str,
    query_fingerprint: str,
    *,
    outcome: RetrievalOutcome = RetrievalOutcome.NOT_CONFIGURED,
) -> PreparedTurnKnowledge:
    """构造无证据的本轮检索结果，使没有配置知识库等情况也有明确的记录。"""
    return PreparedTurnKnowledge(
        turn_id=turn_id,
        turn_token=turn_token,
        outcome=outcome,
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
    """根据绑定数量、成功与失败的库数及最终证据数决定整轮检索状态。"""
    if binding_count == 0:
        return RetrievalOutcome.NOT_CONFIGURED
    if success_count == 0:
        return RetrievalOutcome.UNAVAILABLE
    if failure_count > 0:
        return RetrievalOutcome.PARTIAL
    if evidence_count > 0:
        return RetrievalOutcome.COMPLETE
    return RetrievalOutcome.INSUFFICIENT
