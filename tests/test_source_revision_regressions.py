import asyncio
from pathlib import Path

import pytest

from jdagent.knowledge.catalog import KnowledgeCatalog
from jdagent.knowledge.clock import FakeClock
from jdagent.knowledge.embedding import EmbeddingKind, EmbeddingResult
from jdagent.knowledge.errors import KnowledgeError, KnowledgeErrorCode
from jdagent.knowledge.index import FileKnowledgeIndex, InMemoryKnowledgeIndex, KnowledgeIndex
from jdagent.knowledge.ingestion import KnowledgeIngestion
from jdagent.knowledge.store import ContentAddressedStore
from jdagent.knowledge.types import EmbeddingProfile, SourceLifecycle


@pytest.mark.parametrize("persistent", [False, True])
def test_source_identity_and_unchanged_chunks_survive_replacement(
    tmp_path: Path, persistent: bool
) -> None:
    async def scenario() -> None:
        with KnowledgeCatalog(
            tmp_path / "catalog.sqlite", tmp_path / "backups", clock=FakeClock()
        ) as catalog:
            connection = catalog.register_connection(name="local")
            kb = catalog.create_knowledge_base(connection_id=connection.connection_id, name="docs")
            index: KnowledgeIndex = (
                FileKnowledgeIndex(tmp_path / "index.sqlite")
                if persistent
                else InMemoryKnowledgeIndex()
            )
            try:
                ingestion = KnowledgeIngestion(
                    catalog, ContentAddressedStore(tmp_path / "objects"), index
                )
                first_path = tmp_path / "first.txt"
                second_path = tmp_path / "second.txt"
                first_path.write_text("shared policy", encoding="utf-8")
                second_path.write_text("shared policy", encoding="utf-8")
                first = await ingestion.add_file(kb.knowledge_base_id, first_path)
                second = await ingestion.add_file(kb.knowledge_base_id, second_path)
                hits = index.search_bm25(first.generation_id, second.revision, "shared", 20)
                assert {hit.source_id for hit in hits} == {first.source_id, second.source_id}
                first_path.write_text("shared policy\n\nnew benefit", encoding="utf-8")
                updated = await ingestion.add_file(kb.knowledge_base_id, first_path, replace=True)
                historical = index.search_bm25(first.generation_id, first.revision, "shared", 20)
                assert {hit.source_version_id for hit in historical} == {first.source_version_id}
                current = index.search_bm25(first.generation_id, updated.revision, "shared", 20)
                assert {hit.source_version_id for hit in current} == {
                    updated.source_version_id,
                    second.source_version_id,
                }
            finally:
                if isinstance(index, FileKnowledgeIndex):
                    index.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("persistent", [False, True])
def test_reactivation_never_restores_replaced_versions_or_changes_past_visibility(
    tmp_path: Path, persistent: bool
) -> None:
    async def scenario() -> None:
        with KnowledgeCatalog(
            tmp_path / "catalog.sqlite", tmp_path / "backups", clock=FakeClock()
        ) as catalog:
            connection = catalog.register_connection(name="local")
            kb = catalog.create_knowledge_base(connection_id=connection.connection_id, name="docs")
            index: KnowledgeIndex = (
                FileKnowledgeIndex(tmp_path / "index.sqlite")
                if persistent
                else InMemoryKnowledgeIndex()
            )
            try:
                ingestion = KnowledgeIngestion(
                    catalog, ContentAddressedStore(tmp_path / "objects"), index
                )
                path = tmp_path / "policy.txt"
                path.write_text("obsolete policy", encoding="utf-8")
                first = await ingestion.add_file(kb.knowledge_base_id, path)
                path.write_text("current policy", encoding="utf-8")
                updated = await ingestion.add_file(kb.knowledge_base_id, path, replace=True)
                hidden_revision = await ingestion.deactivate(kb.knowledge_base_id, first.source_id)
                for _ in range(2):
                    active_revision = await ingestion.reactivate(
                        kb.knowledge_base_id, first.source_id
                    )
                    hits = index.search_bm25(first.generation_id, active_revision, "policy", 20)
                    assert [hit.source_version_id for hit in hits] == [updated.source_version_id]
                    await ingestion.deactivate(kb.knowledge_base_id, first.source_id)
                assert index.search_bm25(first.generation_id, hidden_revision, "policy", 20) == ()
                old_hits = index.search_bm25(first.generation_id, first.revision, "policy", 20)
                assert [hit.source_version_id for hit in old_hits] == [first.source_version_id]
            finally:
                if isinstance(index, FileKnowledgeIndex):
                    index.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("action", ["deactivate", "reactivate", "delete"])
def test_lifecycle_rejects_source_from_another_knowledge_base(tmp_path: Path, action: str) -> None:
    async def scenario() -> None:
        with KnowledgeCatalog(
            tmp_path / "catalog.sqlite", tmp_path / "backups", clock=FakeClock()
        ) as catalog:
            connection = catalog.register_connection(name="local")
            owner = catalog.create_knowledge_base(
                connection_id=connection.connection_id, name="owner"
            )
            other = catalog.create_knowledge_base(
                connection_id=connection.connection_id, name="other"
            )
            store = ContentAddressedStore(tmp_path / "objects")
            index = InMemoryKnowledgeIndex()
            ingestion = KnowledgeIngestion(catalog, store, index)
            path = tmp_path / "policy.txt"
            path.write_text("retained policy", encoding="utf-8")
            added = await ingestion.add_file(owner.knowledge_base_id, path)
            await ingestion.add_file(other.knowledge_base_id, path)
            before = catalog.get_knowledge_base(other.knowledge_base_id)
            with pytest.raises(KnowledgeError) as error:
                if action == "delete":
                    await ingestion.delete(other.knowledge_base_id, added.source_id, confirmed=True)
                elif action == "reactivate":
                    await ingestion.reactivate(other.knowledge_base_id, added.source_id)
                else:
                    await ingestion.deactivate(other.knowledge_base_id, added.source_id)
            assert error.value.code is KnowledgeErrorCode.NOT_FOUND
            assert catalog.get_knowledge_base(other.knowledge_base_id) == before
            assert catalog.get_source(added.source_id).lifecycle is SourceLifecycle.ACTIVE
            version = catalog.get_source_version(added.source_version_id)
            assert store.contains(version.raw_hash)

    asyncio.run(scenario())


@pytest.mark.parametrize("persistent", [False, True])
def test_failed_replacement_keeps_current_version_reactivatable(
    tmp_path: Path, persistent: bool
) -> None:
    class FailedEmbedding:
        async def embed(
            self,
            texts: tuple[str, ...],
            profile: EmbeddingProfile,
            *,
            input_kind: EmbeddingKind,
        ) -> EmbeddingResult:
            raise KnowledgeError(KnowledgeErrorCode.PROVIDER_UNAVAILABLE, "Embedding unavailable")

    async def scenario() -> None:
        with KnowledgeCatalog(
            tmp_path / "catalog.sqlite", tmp_path / "backups", clock=FakeClock()
        ) as catalog:
            connection = catalog.register_connection(name="local")
            kb = catalog.create_knowledge_base(connection_id=connection.connection_id, name="docs")
            index: KnowledgeIndex = (
                FileKnowledgeIndex(tmp_path / "index.sqlite")
                if persistent
                else InMemoryKnowledgeIndex()
            )
            try:
                store = ContentAddressedStore(tmp_path / "objects")
                ingestion = KnowledgeIngestion(catalog, store, index)
                path = tmp_path / "policy.txt"
                path.write_text("original policy", encoding="utf-8")
                original = await ingestion.add_file(kb.knowledge_base_id, path)
                path.write_text("replacement policy", encoding="utf-8")
                failed_ingestion = KnowledgeIngestion(catalog, store, index, FailedEmbedding())
                with pytest.raises(KnowledgeError) as error:
                    await failed_ingestion.add_file(kb.knowledge_base_id, path, replace=True)
                assert error.value.code is KnowledgeErrorCode.PROVIDER_UNAVAILABLE
                # A staged replacement must not erase the pointer to the still-visible version.
                assert (
                    catalog.get_source(original.source_id).current_version_id
                    == original.source_version_id
                )
                current = catalog.get_knowledge_base(kb.knowledge_base_id)
                assert current.current_revision == original.revision
                hits = index.search_bm25(
                    original.generation_id, current.current_revision, "policy", 20
                )
                assert [hit.source_version_id for hit in hits] == [original.source_version_id]
                hidden_revision = await ingestion.deactivate(
                    kb.knowledge_base_id, original.source_id
                )
                active_revision = await ingestion.reactivate(
                    kb.knowledge_base_id, original.source_id
                )
                hits = index.search_bm25(original.generation_id, active_revision, "policy", 20)
                assert [hit.source_version_id for hit in hits] == [original.source_version_id]
                assert (
                    index.search_bm25(original.generation_id, hidden_revision, "policy", 20) == ()
                )
            finally:
                if isinstance(index, FileKnowledgeIndex):
                    index.close()

    asyncio.run(scenario())
