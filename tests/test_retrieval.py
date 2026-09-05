import asyncio
from datetime import UTC, datetime
from pathlib import Path

import pytest

from jdagent.knowledge.catalog import KnowledgeCatalog
from jdagent.knowledge.clock import FakeClock
from jdagent.knowledge.embedding import EmbeddingKind, EmbeddingResult, HashEmbedding
from jdagent.knowledge.errors import KnowledgeError, KnowledgeErrorCode
from jdagent.knowledge.index import IndexChunk, InMemoryKnowledgeIndex
from jdagent.knowledge.preparation import CatalogBaseResolver, TurnKnowledgePreparation
from jdagent.knowledge.ptk import (
    BaseQueryStatus,
    FrozenKnowledgeBase,
    QueryFailureReason,
    RetrievalOutcome,
)
from jdagent.knowledge.reranker import ExplodingReranker
from jdagent.knowledge.types import (
    BindingRecord,
    EmbeddingProfile,
    RetrievalProfile,
)


def _binding(kb_id: str) -> BindingRecord:
    now = datetime(2026, 8, 30, tzinfo=UTC)
    return BindingRecord(f"bind_{kb_id}", "ws", "conn_1", kb_id, now)


def _frozen(
    kb_id: str,
    *,
    generation: str = "gen-1",
    revision: int = 1,
    profile: RetrievalProfile | None = None,
    embedding: EmbeddingProfile | None = None,
) -> FrozenKnowledgeBase:
    embedding_profile = embedding or EmbeddingProfile(dimension=8)
    retrieval = profile or RetrievalProfile()
    return FrozenKnowledgeBase(
        f"bind_{kb_id}",
        "conn_1",
        kb_id,
        kb_id,
        generation,
        revision,
        generation,
        embedding_profile,
        retrieval,
        "mixed_zh_en_v1",
    )


def _chunk(
    chunk_id: str,
    text: str,
    *,
    kb_id: str,
    locator: str,
    revision: int = 1,
    parent: str = "parent",
) -> IndexChunk:
    return IndexChunk(
        chunk_id=chunk_id,
        source_id="src",
        source_version_id="sv",
        snapshot_id="snap",
        parent_id=parent,
        locator=locator,
        content_hash=chunk_id,
        text=text,
        vector=(1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0),
        valid_from_revision=revision,
        parent_text=text,
        knowledge_base_id=kb_id,
    )


def test_preparation_outcomes_and_budget() -> None:
    async def scenario() -> None:
        empty = TurnKnowledgePreparation()
        none = await empty.prepare(
            turn_id="t0",
            query_text="q",
            bindings=(),
            frozen=(),
            turn_token="tok0",
        )
        assert none.outcome is RetrievalOutcome.NOT_CONFIGURED

        index = InMemoryKnowledgeIndex()
        index.upsert_chunks(
            "gen-1",
            (_chunk("c1", "年假 15 天", kb_id="hr", locator="md:h2[0]/p[0]"),),
        )
        preparation = TurnKnowledgePreparation(
            embedding=HashEmbedding(),
            indexes={"hr": index},
        )
        complete = await preparation.prepare(
            turn_id="t1",
            query_text="年假",
            bindings=(_binding("hr"),),
            frozen=((_frozen("hr"), None),),
            turn_token="tok1",
        )
        assert complete.outcome is RetrievalOutcome.COMPLETE
        assert complete.evidence[0].reference == "K:tok1:E1"
        expected_fp = RetrievalProfile().fingerprint(
            EmbeddingProfile(dimension=8).fingerprint(),
            "mixed_zh_en_v1",
        )
        assert complete.retrieval_profile_fingerprint == expected_fp

        missing = await preparation.prepare(
            turn_id="t2",
            query_text="年假",
            bindings=(_binding("missing"),),
            frozen=((None, QueryFailureReason.BINDING_INVALID),),
            turn_token="tok2",
        )
        assert missing.outcome is RetrievalOutcome.UNAVAILABLE

        finance = InMemoryKnowledgeIndex()
        partial = await preparation.prepare(
            turn_id="t3",
            query_text="年假",
            bindings=(_binding("hr"), _binding("finance")),
            frozen=(
                (_frozen("hr"), None),
                (_frozen("finance", generation="gen-missing"), None),
            ),
            turn_token="tok3",
        )
        assert partial.outcome is RetrievalOutcome.PARTIAL
        del finance

        unused = InMemoryKnowledgeIndex()
        unused.upsert_chunks(
            "gen-1",
            (_chunk("other", "unrelated vocabulary zzzz", kb_id="hr", locator="txt:p[0]"),),
        )
        insufficient = TurnKnowledgePreparation(indexes={"hr": unused})
        empty_hits = await insufficient.prepare(
            turn_id="t4",
            query_text="年假天数",
            bindings=(_binding("hr"),),
            frozen=((_frozen("hr", profile=RetrievalProfile(mode="bm25")), None),),
            turn_token="tok4",
        )
        assert empty_hits.outcome is RetrievalOutcome.INSUFFICIENT

    asyncio.run(scenario())


def test_reranker_failure_falls_back_without_query_failed() -> None:
    async def scenario() -> None:
        index = InMemoryKnowledgeIndex()
        index.upsert_chunks(
            "gen-1",
            (_chunk("c1", "年假 15 天", kb_id="hr", locator="md:h2[0]/p[0]"),),
        )
        preparation = TurnKnowledgePreparation(
            indexes={"hr": index},
            reranker=ExplodingReranker(),
        )
        ptk = await preparation.prepare(
            turn_id="t1",
            query_text="年假",
            bindings=(_binding("hr"),),
            frozen=((_frozen("hr"), None),),
            turn_token="tok",
        )
        assert ptk.outcome is RetrievalOutcome.COMPLETE
        assert ptk.degradations == ("reranker_fallback",)

    asyncio.run(scenario())


def test_parent_budget_drops_whole_parents() -> None:
    async def scenario() -> None:
        index = InMemoryKnowledgeIndex()
        index.upsert_chunks(
            "gen-1",
            (
                _chunk("a", "short leave", kb_id="hr", locator="txt:p[0]", parent="p0"),
                _chunk("b", "x" * 9000, kb_id="hr", locator="txt:p[1]", parent="p1"),
            ),
        )
        profile = RetrievalProfile(
            parent_limit=1, parent_token_limit=2000, evidence_token_limit=6000
        )
        preparation = TurnKnowledgePreparation(indexes={"hr": index})
        ptk = await preparation.prepare(
            turn_id="t1",
            query_text="leave",
            bindings=(_binding("hr"),),
            frozen=((_frozen("hr", profile=profile), None),),
            turn_token="tok",
        )
        assert len(ptk.evidence) == 1
        assert ptk.evidence[0].locator == "txt:p[0]"

    asyncio.run(scenario())


class _SelectiveBoomEmbedding:
    async def embed(
        self,
        texts: tuple[str, ...],
        profile: EmbeddingProfile,
        *,
        input_kind: EmbeddingKind,
    ) -> EmbeddingResult:
        if profile.model == "bad":
            raise KnowledgeError(KnowledgeErrorCode.PROVIDER_UNAVAILABLE, "boom")
        return await HashEmbedding().embed(texts, profile, input_kind=input_kind)


def test_query_embedding_failure_is_isolated_per_profile() -> None:
    async def scenario() -> None:
        hr = InMemoryKnowledgeIndex()
        finance = InMemoryKnowledgeIndex()
        hr.upsert_chunks(
            "gen-hr",
            (_chunk("c1", "年假 15 天", kb_id="hr", locator="md:h2[0]/p[0]"),),
        )
        finance.upsert_chunks(
            "gen-fin",
            (_chunk("c2", "年假津贴 500", kb_id="finance", locator="md:h2[0]/p[0]"),),
        )
        preparation = TurnKnowledgePreparation(
            embedding=_SelectiveBoomEmbedding(),
            indexes={"hr": hr, "finance": finance},
        )
        ptk = await preparation.prepare(
            turn_id="t-iso",
            query_text="年假",
            bindings=(_binding("hr"), _binding("finance")),
            frozen=(
                (
                    _frozen(
                        "hr",
                        generation="gen-hr",
                        embedding=EmbeddingProfile(model="bad", dimension=8),
                    ),
                    None,
                ),
                (
                    _frozen(
                        "finance",
                        generation="gen-fin",
                        embedding=EmbeddingProfile(model="good", dimension=8),
                    ),
                    None,
                ),
            ),
            turn_token="tok-iso",
        )
        assert ptk.outcome is RetrievalOutcome.PARTIAL
        by_kb = {item.knowledge_base_id: item for item in ptk.bases}
        assert by_kb["hr"].failure_reason is QueryFailureReason.QUERY_FAILED
        assert by_kb["finance"].status is BaseQueryStatus.SUCCEEDED
        assert ptk.evidence

    asyncio.run(scenario())


def test_missing_generation_is_provider_unavailable() -> None:
    async def scenario() -> None:
        preparation = TurnKnowledgePreparation(indexes={"hr": InMemoryKnowledgeIndex()})
        ptk = await preparation.prepare(
            turn_id="t-missing-gen",
            query_text="年假",
            bindings=(_binding("hr"),),
            frozen=((_frozen("hr"), None),),
            turn_token="tok-mg",
        )
        assert ptk.outcome is RetrievalOutcome.UNAVAILABLE
        assert ptk.bases[0].failure_reason is QueryFailureReason.PROVIDER_UNAVAILABLE

    asyncio.run(scenario())


def test_catalog_resolver_missing_connection_is_binding_invalid(tmp_path: Path) -> None:
    catalog = KnowledgeCatalog(tmp_path / "catalog.sqlite", tmp_path / "backups", clock=FakeClock())
    catalog.open()
    connection = catalog.register_connection(name="local")
    kb = catalog.create_knowledge_base(connection_id=connection.connection_id, name="hr")
    now = datetime(2026, 8, 30, tzinfo=UTC)
    stale = BindingRecord("bind_stale", "ws", "conn_missing", kb.knowledge_base_id, now)
    frozen, reason = CatalogBaseResolver(catalog).resolve((stale,))[0]
    assert frozen is None
    assert reason is QueryFailureReason.BINDING_INVALID
    catalog.close()


@pytest.mark.parametrize("bm25_first", [True, False])
@pytest.mark.parametrize("embedding_fails", [True, False])
def test_mixed_modes_share_only_required_query_embedding(
    bm25_first: bool, embedding_fails: bool
) -> None:
    class CountingEmbedding(HashEmbedding):
        calls = 0

        async def embed(
            self, texts: tuple[str, ...], profile: EmbeddingProfile, *, input_kind: EmbeddingKind
        ) -> EmbeddingResult:
            self.calls += 1
            if embedding_fails:
                raise KnowledgeError(KnowledgeErrorCode.PROVIDER_UNAVAILABLE, "offline")
            return await super().embed(texts, profile, input_kind=input_kind)

    async def scenario() -> None:
        embedding = CountingEmbedding()
        indexes: dict[str, InMemoryKnowledgeIndex] = {}
        for kb_id in ("lexical", "hybrid"):
            index = InMemoryKnowledgeIndex()
            index.upsert_chunks(
                "gen-1", (_chunk(kb_id, "leave policy", kb_id=kb_id, locator=kb_id),)
            )
            indexes[kb_id] = index
        ordered = ("lexical", "hybrid") if bm25_first else ("hybrid", "lexical")
        ptk = await TurnKnowledgePreparation(embedding=embedding, indexes=indexes).prepare(
            turn_id="mixed",
            query_text="leave",
            bindings=tuple(_binding(kb_id) for kb_id in ordered),
            frozen=tuple(
                (
                    _frozen(
                        kb_id,
                        profile=RetrievalProfile(
                            mode="bm25" if kb_id == "lexical" else "hybrid_rrf"
                        ),
                    ),
                    None,
                )
                for kb_id in ordered
            ),
        )
        assert embedding.calls == 1
        by_kb = {base.knowledge_base_id: base for base in ptk.bases}
        assert by_kb["lexical"].status is BaseQueryStatus.SUCCEEDED
        assert ptk.outcome is (
            RetrievalOutcome.PARTIAL if embedding_fails else RetrievalOutcome.COMPLETE
        )

    asyncio.run(scenario())
