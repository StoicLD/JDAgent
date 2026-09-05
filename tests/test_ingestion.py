import asyncio
import json
from pathlib import Path

import pytest

from jdagent.knowledge.catalog import KnowledgeCatalog
from jdagent.knowledge.clock import FakeClock
from jdagent.knowledge.errors import KnowledgeError, KnowledgeErrorCode
from jdagent.knowledge.index import IndexChunk, InMemoryKnowledgeIndex
from jdagent.knowledge.ingestion import KnowledgeIngestion
from jdagent.knowledge.store import ContentAddressedStore
from jdagent.knowledge.types import OperationKind, SagaStage, SourceLifecycle

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
        deactivate_rev = kb_after.current_revision
        hidden = index.search_bm25(added.generation_id, deactivate_rev, "带薪年假", top_k=5)
        assert all(hit.source_id != added.source_id for hit in hidden)
        historical = index.search_bm25(added.generation_id, deactivate_rev - 1, "带薪年假", top_k=5)
        assert any(hit.source_id == added.source_id for hit in historical)
        await ingestion.reactivate(kb.knowledge_base_id, added.source_id)
        assert catalog.get_source(added.source_id).lifecycle is SourceLifecycle.ACTIVE
        live_rev = catalog.get_knowledge_base(kb.knowledge_base_id).current_revision
        restored = index.search_bm25(added.generation_id, live_rev, "带薪年假", top_k=5)
        assert any(hit.source_id == added.source_id for hit in restored)
        still_hidden = index.search_bm25(added.generation_id, deactivate_rev, "带薪年假", top_k=5)
        assert all(hit.source_id != added.source_id for hit in still_hidden)
        with pytest.raises(KnowledgeError) as confirm:
            await ingestion.delete(kb.knowledge_base_id, added.source_id, confirmed=False)
        assert confirm.value.code is KnowledgeErrorCode.CONFIRMATION_REQUIRED
        await ingestion.delete(kb.knowledge_base_id, added.source_id, confirmed=True)
        assert catalog.get_source(added.source_id).lifecycle is SourceLifecycle.DELETED
        catalog.close()

    asyncio.run(scenario())


def test_delete_purges_unreferenced_objects_and_keeps_shared(tmp_path: Path) -> None:
    async def scenario() -> None:
        catalog = KnowledgeCatalog(
            tmp_path / "catalog.sqlite", tmp_path / "backups", clock=FakeClock()
        )
        catalog.open()
        connection = catalog.register_connection(name="local")
        hr = catalog.create_knowledge_base(connection_id=connection.connection_id, name="hr")
        finance = catalog.create_knowledge_base(
            connection_id=connection.connection_id, name="finance"
        )
        store = ContentAddressedStore(tmp_path / "objects")
        index = InMemoryKnowledgeIndex()
        ingestion = KnowledgeIngestion(catalog, store, index)
        first = await ingestion.add_file(hr.knowledge_base_id, GOLD / "zh-leave-policy.md")
        second = await ingestion.add_file(finance.knowledge_base_id, GOLD / "zh-leave-policy.md")
        version = catalog.get_source_version(first.source_version_id)
        assert store.contains(version.raw_hash)
        await ingestion.delete(hr.knowledge_base_id, first.source_id, confirmed=True)
        assert catalog.get_source(first.source_id).lifecycle is SourceLifecycle.DELETED
        assert store.contains(version.raw_hash)
        await ingestion.delete(finance.knowledge_base_id, second.source_id, confirmed=True)
        assert catalog.get_source(second.source_id).lifecycle is SourceLifecycle.DELETED
        assert not store.contains(version.raw_hash)
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


def test_expired_lease_cannot_be_stolen_by_a_new_operation(tmp_path: Path) -> None:
    clock = FakeClock()
    catalog = KnowledgeCatalog(tmp_path / "catalog.sqlite", tmp_path / "backups", clock=clock)
    catalog.open()
    catalog.acquire_lease("op-1", "owner-a")
    clock.advance(301)
    with pytest.raises(KnowledgeError) as stolen:
        catalog.acquire_lease("op-2", "owner-b")
    assert stolen.value.code is KnowledgeErrorCode.KNOWLEDGE_BUSY
    catalog.acquire_lease("op-1", "owner-a")
    catalog.release_lease("op-1")
    catalog.acquire_lease("op-1", "owner-a")
    clock.advance(301)
    catalog.acquire_lease("op-2", "owner-b", takeover=True)
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


class _HideChunksIndex(InMemoryKnowledgeIndex):
    def __init__(self) -> None:
        super().__init__()
        self.hide = False
        self.upsert_calls = 0

    def upsert_chunks(self, generation_id: str, chunks: tuple[IndexChunk, ...]) -> None:
        self.upsert_calls += 1
        super().upsert_chunks(generation_id, chunks)

    def get_chunks(self, generation_id: str, chunk_ids: tuple[str, ...]) -> tuple[IndexChunk, ...]:
        if self.hide:
            return ()
        return super().get_chunks(generation_id, chunk_ids)


def test_replace_same_filename_closes_previous_revision(tmp_path: Path) -> None:
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
        replacement = tmp_path / "zh-leave-policy.md"
        replacement.write_bytes((GOLD / "finance-stipend.md").read_bytes())
        replaced = await ingestion.add_file(
            kb.knowledge_base_id,
            replacement,
            replace=True,
        )
        assert replaced.source_id == added.source_id
        assert replaced.revision == added.revision + 1
        current = index.search_bm25(replaced.generation_id, replaced.revision, "stipend", top_k=5)
        assert current
        assert all(hit.source_id == added.source_id for hit in current)
        previous = index.search_bm25(added.generation_id, added.revision, "年假", top_k=5)
        assert any(hit.source_id == added.source_id for hit in previous)
        catalog.close()

    asyncio.run(scenario())


def test_retry_after_index_validation_failure_does_not_bump_revision(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        catalog = KnowledgeCatalog(
            tmp_path / "catalog.sqlite", tmp_path / "backups", clock=FakeClock()
        )
        catalog.open()
        connection = catalog.register_connection(name="local")
        kb = catalog.create_knowledge_base(connection_id=connection.connection_id, name="hr")
        index = _HideChunksIndex()
        index.hide = True
        ingestion = KnowledgeIngestion(catalog, ContentAddressedStore(tmp_path / "objects"), index)
        with pytest.raises(KnowledgeError) as failed:
            await ingestion.add_file(
                kb.knowledge_base_id,
                GOLD / "zh-leave-policy.md",
                operation_id="op_crash_before_active",
            )
        assert failed.value.code is KnowledgeErrorCode.PROVIDER_UNAVAILABLE
        operation = catalog.get_operation("op_crash_before_active")
        assert operation.saga_stage == "indexing"
        assert catalog.get_knowledge_base(kb.knowledge_base_id).current_revision == 0
        index.hide = False
        resumed = await ingestion.retry("op_crash_before_active")
        assert resumed.revision == 1
        assert index.upsert_calls == 1
        catalog.close()

    asyncio.run(scenario())


def test_failed_index_write_blocks_other_mutations_until_recovered(tmp_path: Path) -> None:
    async def scenario() -> None:
        clock = FakeClock()
        with KnowledgeCatalog(
            tmp_path / "catalog.sqlite", tmp_path / "backups", clock=clock
        ) as catalog:
            connection = catalog.register_connection(name="local")
            kb = catalog.create_knowledge_base(connection_id=connection.connection_id, name="kb")
            index = _HideChunksIndex()
            ingestion = KnowledgeIngestion(
                catalog, ContentAddressedStore(tmp_path / "objects"), index
            )
            path = tmp_path / "policy.txt"
            path.write_text("old policy", encoding="utf-8")
            first = await ingestion.add_file(kb.knowledge_base_id, path)
            path.write_text("new policy", encoding="utf-8")
            index.hide = True
            with pytest.raises(KnowledgeError):
                await ingestion.add_file(
                    kb.knowledge_base_id, path, replace=True, operation_id="failed"
                )
            index.hide = False
            clock.advance(301)
            other = tmp_path / "other.txt"
            other.write_text("other policy", encoding="utf-8")
            with pytest.raises(KnowledgeError) as busy:
                await ingestion.add_file(kb.knowledge_base_id, other)
            assert busy.value.code is KnowledgeErrorCode.KNOWLEDGE_BUSY
            assert (
                catalog.get_knowledge_base(kb.knowledge_base_id).current_revision == first.revision
            )
            old = index.search_bm25(first.generation_id, first.revision, "policy", 20)
            assert [hit.source_version_id for hit in old] == [first.source_version_id]
            recovered = await ingestion.retry("failed")
            assert recovered.revision == first.revision + 1
            other_result = await ingestion.add_file(kb.knowledge_base_id, other)
            assert other_result.revision == recovered.revision + 1

    asyncio.run(scenario())


def test_retry_of_legacy_stale_operation_cannot_roll_back_revision(tmp_path: Path) -> None:
    async def scenario() -> None:
        with KnowledgeCatalog(
            tmp_path / "catalog.sqlite", tmp_path / "backups", clock=FakeClock()
        ) as catalog:
            connection = catalog.register_connection(name="local")
            kb = catalog.create_knowledge_base(connection_id=connection.connection_id, name="kb")
            index = _HideChunksIndex()
            index.hide = True
            ingestion = KnowledgeIngestion(
                catalog, ContentAddressedStore(tmp_path / "objects"), index
            )
            with pytest.raises(KnowledgeError):
                await ingestion.add_file(
                    kb.knowledge_base_id, GOLD / "zh-leave-policy.md", operation_id="stale"
                )
            index.hide = False
            payload = json.loads(catalog.get_operation("stale").payload_json)
            catalog.activate_revision(kb.knowledge_base_id, payload["generation_id"], 3)
            before = catalog.get_knowledge_base(kb.knowledge_base_id)
            with pytest.raises(KnowledgeError) as stale:
                await ingestion.retry("stale")
            assert stale.value.code is KnowledgeErrorCode.KNOWLEDGE_BUSY
            assert catalog.get_knowledge_base(kb.knowledge_base_id) == before

    asyncio.run(scenario())


def test_incomplete_index_retry_keeps_original_version_and_checkpoint(tmp_path: Path) -> None:
    class FailedWriteIndex(InMemoryKnowledgeIndex):
        def upsert_chunks(self, generation_id: str, chunks: tuple[IndexChunk, ...]) -> None:
            raise KnowledgeError(KnowledgeErrorCode.PROVIDER_UNAVAILABLE, "write interrupted")

    async def scenario() -> None:
        with KnowledgeCatalog(
            tmp_path / "catalog.sqlite", tmp_path / "backups", clock=FakeClock()
        ) as catalog:
            connection = catalog.register_connection(name="local")
            kb = catalog.create_knowledge_base(connection_id=connection.connection_id, name="kb")
            ingestion = KnowledgeIngestion(
                catalog, ContentAddressedStore(tmp_path / "objects"), FailedWriteIndex()
            )
            with pytest.raises(KnowledgeError):
                await ingestion.add_file(
                    kb.knowledge_base_id, GOLD / "zh-leave-policy.md", operation_id="interrupted"
                )
            operation = catalog.get_operation("interrupted")
            assert operation.saga_stage == "indexing"
            assert operation.source_id is not None
            versions = catalog.list_source_versions(operation.source_id)
            with pytest.raises(KnowledgeError):
                await ingestion.retry("interrupted")
            assert catalog.list_source_versions(operation.source_id) == versions
            assert catalog.get_knowledge_base(kb.knowledge_base_id).current_revision == 0

    asyncio.run(scenario())


def test_legacy_pending_operations_in_separate_bases_can_recover_serially(tmp_path: Path) -> None:
    with KnowledgeCatalog(
        tmp_path / "catalog.sqlite", tmp_path / "backups", clock=FakeClock()
    ) as catalog:
        connection = catalog.register_connection(name="local")
        # Older releases could leave multiple pending operations in a single Catalog.
        for operation_id in ("legacy-a", "legacy-b"):
            kb = catalog.create_knowledge_base(
                connection_id=connection.connection_id, name=operation_id
            )
            catalog.record_operation(
                operation_id=operation_id,
                kind=OperationKind.ADD,
                knowledge_base_id=kb.knowledge_base_id,
                source_id=None,
                stage=SagaStage.INDEX_VALIDATED,
            )
        catalog.acquire_lease("legacy-a", "recovery", takeover=True)
        with pytest.raises(KnowledgeError) as busy:
            catalog.acquire_lease("legacy-b", "recovery", takeover=True)
        assert busy.value.code is KnowledgeErrorCode.KNOWLEDGE_BUSY
        catalog.release_lease("legacy-a")
        catalog.acquire_lease("legacy-b", "recovery", takeover=True)
        catalog.release_lease("legacy-b")
        with pytest.raises(KnowledgeError) as new_write:
            catalog.acquire_lease("new-write", "writer")
        assert new_write.value.code is KnowledgeErrorCode.KNOWLEDGE_BUSY
        catalog.record_operation(
            operation_id="legacy-conflict",
            kind=OperationKind.ADD,
            knowledge_base_id=catalog.get_operation("legacy-a").knowledge_base_id,
            source_id=None,
            stage=SagaStage.INDEXING,
        )
        with pytest.raises(KnowledgeError) as same_base:
            catalog.acquire_lease("legacy-a", "recovery", takeover=True)
        assert same_base.value.code is KnowledgeErrorCode.KNOWLEDGE_BUSY
