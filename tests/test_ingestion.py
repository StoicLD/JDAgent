import asyncio
from pathlib import Path

import pytest

from jdagent.knowledge.catalog import KnowledgeCatalog
from jdagent.knowledge.clock import FakeClock
from jdagent.knowledge.errors import KnowledgeError, KnowledgeErrorCode
from jdagent.knowledge.index import InMemoryKnowledgeIndex
from jdagent.knowledge.ingestion import KnowledgeIngestion
from jdagent.knowledge.store import ContentAddressedStore
from jdagent.knowledge.types import SourceLifecycle

GOLD = Path(__file__).resolve().parents[1] / "testdata" / "v0.3-gold" / "sources"


def test_add_replace_deactivate_and_delete_source_lifecycle(tmp_path: Path) -> None:
    async def scenario() -> None:
        catalog = KnowledgeCatalog(
            tmp_path / "catalog.sqlite", tmp_path / "backups", clock=FakeClock()
        )
        catalog.open()
        connection = catalog.register_connection(name="local")
        kb = catalog.create_knowledge_base(connection_id=connection.connection_id, name="hr")
        index = InMemoryKnowledgeIndex()
        ingestion = KnowledgeIngestion(catalog, ContentAddressedStore(tmp_path / "objects"), index)
        added = await ingestion.add_file(kb.knowledge_base_id, GOLD / "zh-leave-policy.md")
        hits = index.search_bm25(added.generation_id, added.revision, "年假", top_k=5)
        assert hits
        replaced = await ingestion.add_file(
            kb.knowledge_base_id,
            GOLD / "finance-stipend.md",
        )
        assert replaced.source_id != added.source_id
        await ingestion.deactivate(kb.knowledge_base_id, added.source_id)
        source = catalog.get_source(added.source_id)
        assert source.lifecycle is SourceLifecycle.INACTIVE
        kb_after = catalog.get_knowledge_base(kb.knowledge_base_id)
        hidden = index.search_bm25(
            added.generation_id, kb_after.current_revision, "带薪年假", top_k=5
        )
        assert all(hit.source_id != added.source_id for hit in hidden)
        await ingestion.reactivate(kb.knowledge_base_id, added.source_id)
        assert catalog.get_source(added.source_id).lifecycle is SourceLifecycle.ACTIVE
        with pytest.raises(KnowledgeError) as confirm:
            await ingestion.delete(kb.knowledge_base_id, added.source_id, confirmed=False)
        assert confirm.value.code is KnowledgeErrorCode.CONFIRMATION_REQUIRED
        await ingestion.delete(kb.knowledge_base_id, added.source_id, confirmed=True)
        assert catalog.get_source(added.source_id).lifecycle is SourceLifecycle.DELETED
        catalog.close()

    asyncio.run(scenario())


def test_failed_ingest_keeps_previous_revision_readable(tmp_path: Path) -> None:
    async def scenario() -> None:
        catalog = KnowledgeCatalog(
            tmp_path / "catalog.sqlite", tmp_path / "backups", clock=FakeClock()
        )
        catalog.open()
        connection = catalog.register_connection(name="local")
        kb = catalog.create_knowledge_base(connection_id=connection.connection_id, name="hr")
        index = InMemoryKnowledgeIndex()
        ingestion = KnowledgeIngestion(catalog, ContentAddressedStore(tmp_path / "objects"), index)
        added = await ingestion.add_file(kb.knowledge_base_id, GOLD / "en-expense-handbook.txt")
        before = catalog.get_knowledge_base(kb.knowledge_base_id).current_revision
        with pytest.raises(KnowledgeError):
            await ingestion.add_file(kb.knowledge_base_id, tmp_path / "missing.txt")
        after = catalog.get_knowledge_base(kb.knowledge_base_id)
        assert after.current_revision == before
        assert index.search_bm25(added.generation_id, before, "receipt", top_k=3)
        catalog.close()

    asyncio.run(scenario())


def test_lease_rejects_second_writer(tmp_path: Path) -> None:
    catalog = KnowledgeCatalog(tmp_path / "catalog.sqlite", tmp_path / "backups", clock=FakeClock())
    catalog.open()
    catalog.acquire_lease("op-1", "owner-a")
    with pytest.raises(KnowledgeError) as busy:
        catalog.acquire_lease("op-2", "owner-b")
    assert busy.value.code is KnowledgeErrorCode.KNOWLEDGE_BUSY
    catalog.release_lease("op-1")
    catalog.acquire_lease("op-2", "owner-b")
    catalog.close()


def test_expired_lease_can_be_taken_over(tmp_path: Path) -> None:
    clock = FakeClock()
    catalog = KnowledgeCatalog(tmp_path / "catalog.sqlite", tmp_path / "backups", clock=clock)
    catalog.open()
    catalog.acquire_lease("op-1", "owner-a")
    clock.advance(301)
    catalog.acquire_lease("op-2", "owner-b")
    catalog.release_lease("op-2")
    catalog.close()


def test_retry_same_operation_id_is_idempotent(tmp_path: Path) -> None:
    async def scenario() -> None:
        catalog = KnowledgeCatalog(
            tmp_path / "catalog.sqlite", tmp_path / "backups", clock=FakeClock()
        )
        catalog.open()
        connection = catalog.register_connection(name="local")
        kb = catalog.create_knowledge_base(connection_id=connection.connection_id, name="hr")
        index = InMemoryKnowledgeIndex()
        ingestion = KnowledgeIngestion(catalog, ContentAddressedStore(tmp_path / "objects"), index)
        first = await ingestion.add_file(
            kb.knowledge_base_id,
            GOLD / "zh-leave-policy.md",
            operation_id="op_stable_retry",
        )
        second = await ingestion.retry("op_stable_retry")
        assert first.source_id == second.source_id
        assert first.revision == second.revision
        catalog.close()

    asyncio.run(scenario())
