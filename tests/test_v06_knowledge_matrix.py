"""Fixed PTK, citation and source invalidation through the selected composed runtime."""

import asyncio
import hashlib
import json
from dataclasses import replace
from pathlib import Path

import httpx
import pytest

from jdagent.application.coordinator import TurnCoordinator
from jdagent.context_archive import decode_object
from jdagent.domain.errors import StopReason
from jdagent.domain.events import ModelRequestPreparedPayload
from jdagent.domain.json import normalize_json, require_array, require_object
from jdagent.domain.tools import ToolCall
from jdagent.knowledge.catalog import KnowledgeCatalog
from jdagent.knowledge.index import InMemoryKnowledgeIndex
from jdagent.knowledge.ingestion import KnowledgeIngestion
from jdagent.knowledge.ptk import (
    Evidence,
    PreparedTurnKnowledge,
    RetrievalOutcome,
    empty_prepared_knowledge,
)
from jdagent.knowledge.store import ContentAddressedStore
from tests.runtime_matrix import COMBINATIONS, RuntimeCase, model_response


@pytest.mark.parametrize("combination", COMBINATIONS, ids=lambda pair: "/".join(pair))
def test_replaced_or_deleted_knowledge_cannot_be_revived_by_backend_state_or_archive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, combination: tuple[str, str]
) -> None:
    case = RuntimeCase(tmp_path, monkeypatch, combination)
    case.workspace.mkdir()
    phase = "old"
    current: Evidence | None = None
    previous_reference = ""
    remembered = False
    deleted_calls = 0
    requests: list[httpx.Request] = []
    intent_prefix = (
        "Current direct user memory intent; use update_memory with exactly these arguments: "
    )

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal remembered, deleted_calls
        requests.append(request)
        body = json.loads(request.content)
        systems = [m["content"] for m in body["messages"] if m["role"] == "system"]
        bound = next((s for s in systems if intent_prefix in s), None)
        if bound is not None and not remembered:
            remembered = True
            arguments = require_object(
                normalize_json(json.loads(bound.split(intent_prefix, 1)[1])), "intent"
            )
            return model_response("", (ToolCall("save-policy", "update_memory", arguments),))
        if phase == "deleted":
            deleted_calls += 1
            if deleted_calls == 1:
                return model_response(f"Cached answer {previous_reference}.")
            assert "tools" not in body
            return model_response("No currently available evidence.")
        assert current is not None
        assert current.parent_text in "\n".join(systems)
        if phase == "replaced":
            assert "Annual leave is 10 days" not in "\n".join(systems)
        return model_response(f"Annual leave policy {current.reference}.")

    async def scenario() -> None:
        nonlocal phase, current, previous_reference
        paths = case.paths
        catalog = KnowledgeCatalog(paths.knowledge_catalog, paths.knowledge_backups)
        catalog.open()
        try:
            connection = catalog.register_connection(name="local")
            kb = catalog.create_knowledge_base(
                connection_id=connection.connection_id, name="policies"
            )
            objects = ContentAddressedStore(paths.knowledge_objects)
            ingestion = KnowledgeIngestion(catalog, objects, InMemoryKnowledgeIndex())
            source = case.workspace / "policy.md"
            source.write_text("# Policy\n\nAnnual leave is 10 days.\n", encoding="utf-8")
            added = await ingestion.add_file(kb.knowledge_base_id, source)
            source_version = added.source_version_id

            async def prepare(
                coordinator: TurnCoordinator, turn_id: str, turn_token: str, query_text: str
            ) -> PreparedTurnKnowledge:
                nonlocal current, previous_reference
                del coordinator
                base = empty_prepared_knowledge(
                    turn_id,
                    turn_token,
                    hashlib.sha256(query_text.encode()).hexdigest(),
                    outcome=RetrievalOutcome.COMPLETE,
                )
                if phase == "deleted":
                    current = None
                    return base
                version = catalog.get_source_version(source_version)
                snapshot = decode_object(objects.get(version.snapshot_hash))
                parent = require_object(require_array(snapshot["parents"], "parents")[0], "parent")
                child = require_object(require_array(parent["children"], "children")[0], "child")
                current = Evidence(
                    "e1",
                    f"K:{turn_token}:E1",
                    kb.knowledge_base_id,
                    added.source_id,
                    source_version,
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
                previous_reference = current.reference
                return replace(base, evidence=(current,))

            monkeypatch.setattr(TurnCoordinator, "_prepare", prepare)
            async with case.open(handler) as runtime:
                first = await runtime.coordinator.send("What is the leave policy?")
                assert first.result.stop_reason is StopReason.COMPLETED
                assert len(first.result.citations) == 1
                events = [e async for e in runtime.session.read(first.session_id)]
                first_manifest = next(
                    e.payload.object_hash
                    for e in events
                    if isinstance(e.payload, ModelRequestPreparedPayload)
                )
                initial = await runtime.reconstruct_request(first_manifest)
                assert any("Annual leave is 10 days" in p.content for p in initial.system_parts)
                saved = await runtime.coordinator.send(
                    "记住：Annual leave is 10 days", session_id=first.session_id
                )
                assert (
                    saved.result.stop_reason is StopReason.COMPLETED
                    and saved.result.tool_calls == 1
                )
                assert runtime.maintenance is not None
                memory = runtime.maintenance.memory
                assert (await memory.search_memory("leave"))["total"] == 1
                source.write_text("# Policy\n\nAnnual leave is 12 days.\n", encoding="utf-8")
                replacement = await ingestion.add_file(kb.knowledge_base_id, source, replace=True)
                source_version = replacement.source_version_id
                phase = "replaced"
                assert (await memory.search_memory("leave"))["total"] == 0
                assert (
                    memory.store.recall(paths.identity, include_inactive=True)[0].state
                    == "needs_verification"
                )
                # Replacement retains historical source versions; deletion tombstones them.
                assert await runtime.reconstruct_request(first_manifest) == initial
                revised = await runtime.coordinator.send(
                    "What does the revised policy say?", session_id=first.session_id
                )
                assert (
                    revised.result.stop_reason is StopReason.COMPLETED
                    and len(revised.result.citations) == 1
                )
                assert (
                    revised.result.citations[0].source_version_id == replacement.source_version_id
                )
                await ingestion.delete(kb.knowledge_base_id, added.source_id, confirmed=True)
                phase = "deleted"
                with pytest.raises(ValueError, match="tombstoned"):
                    await runtime.reconstruct_request(first_manifest)
                last = await runtime.coordinator.send(
                    "Can you still cite that source?", session_id=first.session_id
                )
                assert last.result.stop_reason is StopReason.COMPLETED
                assert last.result.model_calls == 2 and last.result.citations == ()
                assert last.result.assistant_text == "No currently available evidence."
                assert (await memory.search_memory("leave"))["total"] == 0
        finally:
            catalog.close()

    asyncio.run(scenario())
    assert len(requests) == 6 and deleted_calls == 2
