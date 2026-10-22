"""Resolve archived evidence through the owning KB's version and lifecycle contracts."""

import asyncio
import hashlib
from dataclasses import replace
from pathlib import Path

from jdagent.context_archive import decode_object
from jdagent.domain.json import (
    JsonObject,
    require_array,
    require_integer,
    require_object,
    require_string,
)
from jdagent.knowledge.catalog import KnowledgeCatalog
from jdagent.knowledge.citation import evidence_system_part
from jdagent.knowledge.errors import KnowledgeError
from jdagent.knowledge.ptk import Evidence, RetrievalOutcome, empty_prepared_knowledge
from jdagent.knowledge.store import ContentAddressedStore
from jdagent.knowledge.types import SourceLifecycle


class KnowledgeReferences:
    def __init__(self, catalog: Path, backups: Path, objects: Path) -> None:
        self.catalog_path, self.backups, self.objects = catalog, backups, objects

    def _text(self, reference: JsonObject, *, current: bool) -> str:
        if not self.catalog_path.is_file():
            raise ValueError("Knowledge dependency is unavailable")
        catalog = KnowledgeCatalog(self.catalog_path, self.backups)
        try:
            catalog.open()
            source = catalog.get_source(require_string(reference, "source_id"))
            if source.lifecycle in {SourceLifecycle.DELETE_PENDING, SourceLifecycle.DELETED}:
                raise ValueError("Knowledge dependency is tombstoned")
            if source.knowledge_base_id != require_string(reference, "knowledge_base_id"):
                raise ValueError("Knowledge dependency belongs to a different KB")
            version_id = require_string(reference, "source_version_id")
            if current and source.current_version_id != version_id:
                raise ValueError("Knowledge dependency version changed")
            if current and source.lifecycle is not SourceLifecycle.ACTIVE:
                raise ValueError("Knowledge dependency is inactive")
            version = catalog.get_source_version(version_id)
            if version.source_id != source.source_id:
                raise ValueError("Knowledge version belongs to a different source")
            if require_string(reference, "snapshot_id") != f"snap_{version.snapshot_hash[:16]}":
                raise ValueError("Knowledge snapshot identity mismatch")
            snapshot = decode_object(ContentAddressedStore(self.objects).get(version.snapshot_hash))
            if snapshot.get("locator_schema_version") != reference.get("locator_schema_version"):
                raise ValueError("Knowledge locator schema changed")
            for raw in require_array(snapshot.get("parents"), "parents"):
                parent = require_object(raw, "parent")
                for item in require_array(parent.get("children"), "children"):
                    child = require_object(item, "child")
                    if child.get("locator") == reference.get("locator"):
                        digest = hashlib.sha256(require_string(child, "text").encode()).hexdigest()
                        if digest != reference.get("content_hash"):
                            raise ValueError("Knowledge evidence content hash mismatch")
                        return require_string(parent, "text")
            raise ValueError("Knowledge evidence locator is missing")
        finally:
            catalog.close()

    async def valid_dependency(self, dependency: str) -> bool:
        try:
            await asyncio.to_thread(self._text, decode_object(dependency.encode()), current=True)
            return True
        except (ValueError, KnowledgeError, OSError):
            return False

    async def render(self, entry: JsonObject) -> str:
        if entry.get("render_version") != 1:
            raise ValueError("Unsupported knowledge rendering version")
        evidence: list[Evidence] = []
        for raw in require_array(entry.get("references"), "references"):
            item = require_object(raw, "reference")
            text = await asyncio.to_thread(self._text, item, current=False)
            evidence.append(
                Evidence(
                    require_string(item, "evidence_id"),
                    require_string(item, "reference"),
                    require_string(item, "knowledge_base_id"),
                    require_string(item, "source_id"),
                    require_string(item, "source_version_id"),
                    require_string(item, "snapshot_id"),
                    require_string(item, "locator"),
                    require_integer(item, "locator_schema_version"),
                    require_string(item, "content_hash"),
                    text,
                    require_integer(item, "token_estimate"),
                    require_integer(item, "ordinal"),
                    require_string(item, "kb_name"),
                    require_string(item, "source_name"),
                )
            )
        prepared = empty_prepared_knowledge(
            "",
            require_string(entry, "turn_token"),
            "",
            outcome=RetrievalOutcome(require_string(entry, "outcome")),
        )
        degradations = require_array(entry.get("degradations", []), "degradations")
        if not all(isinstance(item, str) for item in degradations):
            raise ValueError("Invalid archived knowledge degradation")
        return evidence_system_part(
            replace(
                prepared,
                evidence=tuple(evidence),
                degradations=tuple(str(item) for item in degradations),
            )
        )
