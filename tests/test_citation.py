from pathlib import Path

from jdagent.domain.events import CitationRecord
from jdagent.knowledge.catalog import KnowledgeCatalog
from jdagent.knowledge.citation import (
    CitationTarget,
    citation_target,
    evidence_system_part,
    finalize_answer,
    resolve_source_citation_target,
)
from jdagent.knowledge.clock import FakeClock
from jdagent.knowledge.index import InMemoryKnowledgeIndex
from jdagent.knowledge.ingestion import KnowledgeIngestion
from jdagent.knowledge.ptk import (
    Evidence,
    EvidenceBudget,
    PreparedTurnKnowledge,
    RetrievalOutcome,
)
from jdagent.knowledge.store import ContentAddressedStore
from jdagent.knowledge.types import SourceLifecycle


def _ptk() -> PreparedTurnKnowledge:
    evidence = Evidence(
        "ev_1",
        "K:tok:E1",
        "kb",
        "src",
        "sv",
        "snap",
        "md:h2[0]/p[0]",
        1,
        "hash",
        "年假 15 天",
        4,
        1,
    )
    return PreparedTurnKnowledge(
        "turn-1",
        "tok",
        RetrievalOutcome.COMPLETE,
        "fp",
        (),
        (evidence,),
        EvidenceBudget(20, 20, 1, 40, 1, 4, 6000, 8),
        (),
        "ret",
    )


def test_finalize_rewrites_machine_references_and_rejects_unknown() -> None:
    knowledge = _ptk()
    ok = finalize_answer("Leave is 15 days K:tok:E1.", knowledge)
    assert ok.illegal is False
    assert ok.display_text == "Leave is 15 days [1]."
    assert ok.citations == (
        CitationRecord(1, "ev_1", "kb", "src", "sv", "snap", "md:h2[0]/p[0]", 1, "hash"),
    )
    bad = finalize_answer("See K:other:E1 and [1].", knowledge)
    assert bad.illegal is True


def test_evidence_prompt_requires_exact_machine_references() -> None:
    prompt = evidence_system_part(_ptk())
    assert "K:tok:E1" in prompt
    assert "machine reference" in prompt
    assert "[1]" in prompt
    assert "kb=" in prompt
    assert "source=" in prompt
    assert "version=sv" in prompt


def test_citation_target_distinguishes_openable_tombstone_and_corrupt() -> None:
    assert citation_target(SourceLifecycle.ACTIVE, object_missing=False) is CitationTarget.OPENABLE
    assert (
        citation_target(SourceLifecycle.INACTIVE, object_missing=False) is CitationTarget.OPENABLE
    )
    assert (
        citation_target(SourceLifecycle.DELETE_PENDING, object_missing=False)
        is CitationTarget.TOMBSTONE
    )
    assert (
        citation_target(SourceLifecycle.DELETED, object_missing=False) is CitationTarget.TOMBSTONE
    )
    assert citation_target(SourceLifecycle.ACTIVE, object_missing=True) is CitationTarget.CORRUPT
    assert citation_target(SourceLifecycle.DELETED, object_missing=True) is CitationTarget.TOMBSTONE


def test_citation_resolver_uses_catalog_lifecycle_and_store(tmp_path: Path) -> None:
    import asyncio

    gold = Path(__file__).resolve().parents[1] / "testdata" / "v0.3-gold" / "sources"

    async def scenario() -> None:
        catalog = KnowledgeCatalog(
            tmp_path / "catalog.sqlite", tmp_path / "backups", clock=FakeClock()
        )
        catalog.open()
        connection = catalog.register_connection(name="local")
        kb = catalog.create_knowledge_base(connection_id=connection.connection_id, name="hr")
        store = ContentAddressedStore(tmp_path / "objects")
        ingestion = KnowledgeIngestion(catalog, store, InMemoryKnowledgeIndex())
        added = await ingestion.add_file(kb.knowledge_base_id, gold / "zh-leave-policy.md")
        assert (
            resolve_source_citation_target(catalog, store, added.source_id)
            is CitationTarget.OPENABLE
        )
        await ingestion.deactivate(kb.knowledge_base_id, added.source_id)
        assert (
            resolve_source_citation_target(catalog, store, added.source_id)
            is CitationTarget.OPENABLE
        )
        await ingestion.delete(kb.knowledge_base_id, added.source_id, confirmed=True)
        assert (
            resolve_source_citation_target(catalog, store, added.source_id)
            is CitationTarget.TOMBSTONE
        )
        catalog.close()

    asyncio.run(scenario())
