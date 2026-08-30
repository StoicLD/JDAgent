"""Source ingest saga, activation, and reversible lifecycle operations."""

from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import cast
from uuid import uuid4

from jdagent.knowledge.catalog import KnowledgeCatalog, new_id
from jdagent.knowledge.chunking import chunk_document
from jdagent.knowledge.embedding import EmbeddingKind, EmbeddingPort, HashEmbedding
from jdagent.knowledge.errors import KnowledgeError, KnowledgeErrorCode
from jdagent.knowledge.index import IndexChunk, KnowledgeIndex
from jdagent.knowledge.parsing import format_from_path, parse_source
from jdagent.knowledge.store import ContentAddressedStore, content_digest
from jdagent.knowledge.types import OperationKind, OperationRecord, SagaStage, SourceLifecycle


@dataclass(frozen=True, slots=True)
class IngestResult:
    operation_id: str
    source_id: str
    source_version_id: str
    revision: int
    generation_id: str


class KnowledgeIngestion:
    """Run per-file all-or-nothing ingest against Catalog, Store, and Index."""

    def __init__(
        self,
        catalog: KnowledgeCatalog,
        store: ContentAddressedStore,
        index: KnowledgeIndex,
        embedding: EmbeddingPort | None = None,
        *,
        owner: str = "writer",
    ) -> None:
        self._catalog = catalog
        self._store = store
        self._index = index
        self._embedding = embedding or HashEmbedding()
        self._owner = owner

    async def add_file(
        self,
        knowledge_base_id: str,
        path: Path,
        *,
        operation_id: str | None = None,
        encoding: str | None = None,
        replace: bool = False,
    ) -> IngestResult:
        operation = operation_id or new_id("op")
        completed = self._completed_result(operation)
        if completed is not None:
            return completed
        try:
            data = await asyncio.to_thread(Path.read_bytes, path)
        except OSError as error:
            raise KnowledgeError(
                KnowledgeErrorCode.NOT_FOUND,
                "Source file could not be read",
            ) from error
        name = path.name
        source_format = format_from_path(name)
        raw_hash = content_digest(data)
        existing = self._catalog.get_source_by_name(knowledge_base_id, name)
        if existing is not None and not replace:
            if existing.lifecycle is not SourceLifecycle.DELETED:
                if (
                    existing.lifecycle is SourceLifecycle.ACTIVE
                    and existing.current_version_id is not None
                ):
                    version = self._catalog.get_source_version(existing.current_version_id)
                    if version.raw_hash == raw_hash:
                        kb = self._catalog.get_knowledge_base(knowledge_base_id)
                        generation_id = kb.current_generation_id or ""
                        return IngestResult(
                            operation,
                            existing.source_id,
                            existing.current_version_id,
                            kb.current_revision,
                            generation_id,
                        )
                raise KnowledgeError(
                    KnowledgeErrorCode.NAME_CONFLICT,
                    "Source already exists; use replace",
                )
        if existing is None and replace:
            raise KnowledgeError(KnowledgeErrorCode.NOT_FOUND, "Source not found")
        kind = OperationKind.REPLACE if replace else OperationKind.ADD
        self._catalog.acquire_lease(operation, self._owner)
        try:
            self._catalog.record_operation(
                operation_id=operation,
                kind=kind,
                knowledge_base_id=knowledge_base_id,
                source_id=None if existing is None else existing.source_id,
                stage=SagaStage.RECEIVED,
                payload_json=json.dumps(
                    {"path": str(path), "replace": replace, "encoding": encoding}
                ),
            )
            kb = self._catalog.get_knowledge_base(knowledge_base_id)
            stored_hash = self._store.put(data)
            self._catalog.record_operation(
                operation_id=operation,
                kind=kind,
                knowledge_base_id=knowledge_base_id,
                source_id=None if existing is None else existing.source_id,
                stage=SagaStage.RAW_STORED,
                payload_json=json.dumps({"raw_hash": stored_hash, "path": str(path)}),
            )
            parsed = parse_source(data, source_format=source_format, encoding=encoding)
            parents = chunk_document(parsed)
            snapshot_payload = json.dumps(
                {
                    "parser_profile": parsed.parser_profile,
                    "locator_schema_version": parsed.locator_schema_version,
                    "parents": [
                        {
                            "parent_id": parent.parent_id,
                            "locator": parent.locator,
                            "text": parent.text,
                            "children": [
                                {
                                    "chunk_id": child.chunk_id,
                                    "locator": child.locator,
                                    "text": child.text,
                                    "overlapped": child.overlapped,
                                }
                                for child in parent.children
                            ],
                        }
                        for parent in parents
                    ],
                },
                ensure_ascii=False,
                sort_keys=True,
            ).encode("utf-8")
            snapshot_hash = self._store.put(snapshot_payload)
            self._catalog.record_operation(
                operation_id=operation,
                kind=kind,
                knowledge_base_id=knowledge_base_id,
                source_id=None if existing is None else existing.source_id,
                stage=SagaStage.PARSED,
                payload_json=json.dumps({"snapshot_hash": snapshot_hash}),
            )
            source = self._catalog.upsert_source(
                knowledge_base_id=knowledge_base_id,
                name=name,
                source_id=None if existing is None else existing.source_id,
                lifecycle=(existing.lifecycle if existing is not None else SourceLifecycle.ACTIVE),
            )
            version_id = self._catalog.add_source_version(
                source_id=source.source_id,
                raw_hash=stored_hash,
                encoding=parsed.encoding,
                encoding_method=parsed.encoding_method,
                snapshot_hash=snapshot_hash,
                parser_profile=parsed.parser_profile,
            )
            generation_id = kb.current_generation_id or new_id("gen")
            next_revision = kb.current_revision + 1
            children = [(parent, child) for parent in parents for child in parent.children]
            texts = tuple(child.text for _parent, child in children)
            embedded = await self._embedding.embed(
                texts,
                kb.embedding_profile,
                input_kind=EmbeddingKind.DOCUMENT,
            )
            self._catalog.record_operation(
                operation_id=operation,
                kind=kind,
                knowledge_base_id=knowledge_base_id,
                source_id=source.source_id,
                stage=SagaStage.INDEXING,
            )
            snapshot_id = f"snap_{snapshot_hash[:16]}"
            index_chunks = tuple(
                IndexChunk(
                    chunk_id=child.chunk_id,
                    source_id=source.source_id,
                    source_version_id=version_id,
                    snapshot_id=snapshot_id,
                    parent_id=child.parent_id,
                    locator=child.locator,
                    content_hash=hashlib.sha256(child.text.encode("utf-8")).hexdigest(),
                    text=child.text,
                    vector=embedded.vectors[index],
                    valid_from_revision=next_revision,
                    parent_text=parent.text,
                    locator_schema_version=parsed.locator_schema_version,
                    knowledge_base_id=knowledge_base_id,
                )
                for index, (parent, child) in enumerate(children)
            )
            if replace and kb.current_generation_id is not None:
                old_ids = self._catalog.chunks_for_source(
                    kb.current_generation_id, source.source_id
                )
                self._index.close_chunks(kb.current_generation_id, old_ids, next_revision)
            self._index.upsert_chunks(generation_id, index_chunks)
            self._catalog.remember_chunks(
                generation_id,
                source.source_id,
                tuple(chunk.chunk_id for chunk in index_chunks),
            )
            self._catalog.record_operation(
                operation_id=operation,
                kind=kind,
                knowledge_base_id=knowledge_base_id,
                source_id=source.source_id,
                stage=SagaStage.INDEX_VALIDATED,
            )
            self._catalog.activate_revision(knowledge_base_id, generation_id, next_revision)
            self._catalog.set_source_lifecycle(
                source.source_id,
                SourceLifecycle.ACTIVE,
                current_version_id=version_id,
            )
            result = IngestResult(
                operation, source.source_id, version_id, next_revision, generation_id
            )
            self._catalog.record_operation(
                operation_id=operation,
                kind=kind,
                knowledge_base_id=knowledge_base_id,
                source_id=source.source_id,
                stage=SagaStage.ACTIVE,
                payload_json=json.dumps(
                    {
                        "source_id": result.source_id,
                        "source_version_id": result.source_version_id,
                        "revision": result.revision,
                        "generation_id": result.generation_id,
                        "path": str(path),
                        "replace": replace,
                    }
                ),
            )
            return result
        except KnowledgeError as error:
            self._record_failure(operation, kind, knowledge_base_id, existing, error)
            raise
        finally:
            self._catalog.release_lease(operation)

    async def retry(self, operation_id: str) -> IngestResult:
        completed = self._completed_result(operation_id)
        if completed is not None:
            return completed
        record = self._catalog.get_operation(operation_id)
        payload = _payload(record)
        path_value = payload.get("path")
        if not isinstance(path_value, str) or not path_value:
            raise KnowledgeError(
                KnowledgeErrorCode.INVALID_ARGUMENT,
                "Operation cannot be retried without a stored path",
            )
        replace = payload.get("replace") is True
        encoding_value = payload.get("encoding")
        encoding = encoding_value if isinstance(encoding_value, str) else None
        return await self.add_file(
            record.knowledge_base_id,
            Path(path_value),
            operation_id=operation_id,
            encoding=encoding,
            replace=replace,
        )

    def reconcile(self) -> dict[str, int]:
        expired = self._catalog.expire_stale_leases()
        return {"expired_leases": expired}

    async def deactivate(self, knowledge_base_id: str, source_id: str) -> int:
        return await self._visibility(knowledge_base_id, source_id, SourceLifecycle.INACTIVE)

    async def reactivate(self, knowledge_base_id: str, source_id: str) -> int:
        source = self._catalog.get_source(source_id)
        if source.lifecycle is SourceLifecycle.DELETED:
            raise KnowledgeError(
                KnowledgeErrorCode.NOT_FOUND,
                "Deleted source cannot be reactivated",
            )
        kb = self._catalog.get_knowledge_base(knowledge_base_id)
        generation_id = kb.current_generation_id
        if generation_id is None:
            raise KnowledgeError(
                KnowledgeErrorCode.NOT_FOUND,
                "Knowledge base has no generation",
            )
        operation = new_id("op")
        self._catalog.acquire_lease(operation, self._owner)
        try:
            next_revision = kb.current_revision + 1
            chunk_ids = self._catalog.chunks_for_source(generation_id, source_id)
            self._index.close_chunks(generation_id, chunk_ids, 0)
            self._catalog.activate_revision(knowledge_base_id, generation_id, next_revision)
            self._catalog.set_source_lifecycle(source_id, SourceLifecycle.ACTIVE)
            return next_revision
        finally:
            self._catalog.release_lease(operation)

    async def delete(self, knowledge_base_id: str, source_id: str, *, confirmed: bool) -> None:
        if not confirmed:
            raise KnowledgeError(
                KnowledgeErrorCode.CONFIRMATION_REQUIRED,
                "Deleting a source requires confirmation",
            )
        source = self._catalog.get_source(source_id)
        if source.current_version_id is not None:
            version = self._catalog.get_source_version(source.current_version_id)
            if version.raw_hash and not self._store.contains(version.raw_hash):
                raise KnowledgeError(
                    KnowledgeErrorCode.SOURCE_CORRUPT,
                    "Source object is missing",
                )
        kb = self._catalog.get_knowledge_base(knowledge_base_id)
        generation_id = kb.current_generation_id
        operation = new_id("op")
        self._catalog.acquire_lease(operation, self._owner)
        try:
            next_revision = kb.current_revision + 1
            if generation_id is not None:
                chunk_ids = self._catalog.chunks_for_source(generation_id, source_id)
                self._index.close_chunks(generation_id, chunk_ids, next_revision)
                self._catalog.activate_revision(knowledge_base_id, generation_id, next_revision)
            self._catalog.set_source_lifecycle(source_id, SourceLifecycle.DELETE_PENDING)
            self._catalog.set_source_lifecycle(source_id, SourceLifecycle.DELETED)
        finally:
            self._catalog.release_lease(operation)

    async def _visibility(
        self, knowledge_base_id: str, source_id: str, lifecycle: SourceLifecycle
    ) -> int:
        self._catalog.get_source(source_id)
        kb = self._catalog.get_knowledge_base(knowledge_base_id)
        generation_id = kb.current_generation_id
        if generation_id is None:
            raise KnowledgeError(
                KnowledgeErrorCode.NOT_FOUND,
                "Knowledge base has no generation",
            )
        operation = new_id("op")
        self._catalog.acquire_lease(operation, self._owner)
        try:
            next_revision = kb.current_revision + 1
            chunk_ids = self._catalog.chunks_for_source(generation_id, source_id)
            self._index.close_chunks(generation_id, chunk_ids, next_revision)
            self._catalog.activate_revision(knowledge_base_id, generation_id, next_revision)
            self._catalog.set_source_lifecycle(source_id, lifecycle)
            return next_revision
        finally:
            self._catalog.release_lease(operation)

    def _completed_result(self, operation_id: str) -> IngestResult | None:
        try:
            record = self._catalog.get_operation(operation_id)
        except KnowledgeError:
            return None
        if record.saga_stage != SagaStage.ACTIVE.value:
            return None
        payload = _payload(record)
        source_id = payload.get("source_id")
        version_id = payload.get("source_version_id")
        revision = payload.get("revision")
        generation_id = payload.get("generation_id")
        if (
            not isinstance(source_id, str)
            or not isinstance(version_id, str)
            or not isinstance(revision, int)
            or not isinstance(generation_id, str)
        ):
            return None
        return IngestResult(operation_id, source_id, version_id, revision, generation_id)

    def _record_failure(
        self,
        operation: str,
        kind: OperationKind,
        knowledge_base_id: str,
        existing: object,
        error: KnowledgeError,
    ) -> None:
        source_id = getattr(existing, "source_id", None)
        try:
            self._catalog.record_operation(
                operation_id=operation,
                kind=kind,
                knowledge_base_id=knowledge_base_id,
                source_id=source_id if isinstance(source_id, str) else None,
                stage=SagaStage.RECEIVED,
                error_code=error.code.value,
                error_message=str(error),
            )
        except KnowledgeError:
            return


def _payload(record: OperationRecord) -> dict[str, object]:
    try:
        loaded = json.loads(record.payload_json)
    except json.JSONDecodeError:
        return {}
    if not isinstance(loaded, dict):
        return {}
    return cast(dict[str, object], loaded)


def owner_token() -> str:
    return f"writer-{uuid4().hex[:8]}"
