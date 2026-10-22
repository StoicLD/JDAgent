import asyncio
import hashlib
from dataclasses import asdict, replace
from pathlib import Path

import pytest

from jdagent.adapters.jsonl_session import JsonlSession
from jdagent.adapters.memory import InMemoryContextObjects
from jdagent.context_archive import archive_request, decode_object, encode_json, reconstruct_request
from jdagent.domain.model import MessageRole, ModelMessage, ModelRequest, SystemPart
from jdagent.knowledge.catalog import KnowledgeCatalog
from jdagent.knowledge.citation import evidence_system_part
from jdagent.knowledge.context_references import KnowledgeReferences
from jdagent.knowledge.index import InMemoryKnowledgeIndex
from jdagent.knowledge.ingestion import KnowledgeIngestion
from jdagent.knowledge.ptk import Evidence, RetrievalOutcome, empty_prepared_knowledge
from jdagent.knowledge.store import ContentAddressedStore
from jdagent.memory_service import MemoryService
from jdagent.memory_store import MemoryStore


def test_kb_reconstruction_and_memory_respect_replace_and_delete(tmp_path: Path):
    async def scenario():
        catalog = KnowledgeCatalog(tmp_path / "catalog.sqlite", tmp_path / "backups")
        catalog.open()
        connection = catalog.register_connection(name="local")
        kb = catalog.create_knowledge_base(connection_id=connection.connection_id, name="policies")
        owner = ContentAddressedStore(tmp_path / "kb-objects")
        ingestion = KnowledgeIngestion(catalog, owner, InMemoryKnowledgeIndex())
        source_file = tmp_path / "policy.md"
        source_file.write_text("# Policy\n\nAnnual leave is 10 days.\n", encoding="utf-8")
        added = await ingestion.add_file(kb.knowledge_base_id, source_file)
        version = catalog.get_source_version(added.source_version_id)
        snapshot = decode_object(owner.get(version.snapshot_hash))
        parents = snapshot["parents"]
        assert isinstance(parents, list) and isinstance(parents[0], dict)
        parent = parents[0]
        children = parent["children"]
        assert isinstance(children, list) and isinstance(children[0], dict)
        child = children[0]
        evidence = Evidence(
            "e1",
            "ref1",
            kb.knowledge_base_id,
            added.source_id,
            added.source_version_id,
            f"snap_{version.snapshot_hash[:16]}",
            str(child["locator"]),
            int(str(snapshot["locator_schema_version"])),
            hashlib.sha256(str(child["text"]).encode()).hexdigest(),
            str(parent["text"]),
            20,
            1,
            "policies",
            "policy.md",
        )
        prepared = replace(
            empty_prepared_knowledge("t", "token", "query", outcome=RetrievalOutcome.COMPLETE),
            evidence=(evidence,),
        )
        request = ModelRequest(
            "fake",
            (ModelMessage(MessageRole.USER, "leave policy?"),),
            (SystemPart(evidence_system_part(prepared), "untrusted_evidence"),),
        )
        objects = InMemoryContextObjects()
        manifest = await archive_request(request, objects, prepared)
        resolver = KnowledgeReferences(
            tmp_path / "catalog.sqlite", tmp_path / "backups", tmp_path / "kb-objects"
        )
        assert (
            await reconstruct_request(manifest, objects, resolve_knowledge=resolver.render)
            == request
        )
        dependency = encode_json(
            {key: value for key, value in asdict(evidence).items() if key != "parent_text"}
        ).decode()
        session = JsonlSession(tmp_path / "sessions")
        memory = MemoryService(
            store=MemoryStore(tmp_path / "memory.sqlite"),
            session=session,
            discovery=session,
            workspace="w",
            write_allowed=lambda: True,
            knowledge_references=resolver,
        )
        intent = memory.store.accept_intent(
            "记住：Annual leave is 10 days",
            workspace="w",
            session="s",
            turn="t",
            event="e",
            dependencies=(dependency,),
        )
        assert intent
        memory.store.apply_intent(
            intent,
            workspace="w",
            session="s",
            turn="t",
            write_allowed=True,
            dependencies_verified=True,
        )
        assert (await memory.search_memory("leave"))["total"] == 1
        source_file.write_text("# Policy\n\nAnnual leave is 12 days.\n", encoding="utf-8")
        await ingestion.add_file(kb.knowledge_base_id, source_file, replace=True)
        assert (await memory.search_memory("leave"))["total"] == 0
        assert memory.store.recall("w", include_inactive=True)[0].state == "needs_verification"
        assert (
            await reconstruct_request(manifest, objects, resolve_knowledge=resolver.render)
            == request
        )
        await ingestion.delete(kb.knowledge_base_id, added.source_id, confirmed=True)
        with pytest.raises(ValueError, match="tombstoned"):
            await reconstruct_request(manifest, objects, resolve_knowledge=resolver.render)
        catalog.close()

    asyncio.run(scenario())
