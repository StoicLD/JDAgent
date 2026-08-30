"""Milvus Standalone adapter; SDK types stay inside this module."""

from __future__ import annotations

# pymilvus is an optional extra; this adapter keeps SDK types private.
# pyright: reportMissingImports=false, reportUnknownMemberType=false
# pyright: reportUnknownVariableType=false, reportUnknownArgumentType=false
# pyright: reportUnknownLambdaType=false
from typing import Any, cast

from jdagent.knowledge.errors import KnowledgeError, KnowledgeErrorCode
from jdagent.knowledge.index import IndexChunk, IndexHit, KnowledgeIndex, bm25_score, tokenize


class MilvusKnowledgeIndex:
    """Dense HNSW + filtered search. Alias is never used as the read target."""

    def __init__(self, uri: str) -> None:
        try:
            from pymilvus import Collection, connections, utility
        except ImportError as error:
            raise KnowledgeError(
                KnowledgeErrorCode.DEPENDENCY_MISSING,
                "pymilvus is required for the Milvus knowledge index",
            ) from error
        self._uri = uri
        self._connections = connections
        self._utility = utility
        self._collection_type = Collection
        self._alias = f"jdagent-{id(self)}"
        try:
            connections.connect(alias=self._alias, uri=uri)
        except Exception as error:
            raise KnowledgeError(
                KnowledgeErrorCode.PROVIDER_UNAVAILABLE,
                "Milvus is unavailable",
            ) from error

    def upsert_chunks(self, generation_id: str, chunks: tuple[IndexChunk, ...]) -> None:
        collection = self._ensure_collection(
            generation_id, chunks[0].vector.__len__() if chunks else 8
        )
        if not chunks:
            return
        payload = [
            [chunk.chunk_id for chunk in chunks],
            [chunk.source_id for chunk in chunks],
            [chunk.source_version_id for chunk in chunks],
            [chunk.snapshot_id for chunk in chunks],
            [chunk.parent_id for chunk in chunks],
            [chunk.locator for chunk in chunks],
            [chunk.content_hash for chunk in chunks],
            [chunk.text for chunk in chunks],
            [list(chunk.vector) for chunk in chunks],
            [chunk.valid_from_revision for chunk in chunks],
            [chunk.valid_to_revision for chunk in chunks],
            [chunk.parent_text for chunk in chunks],
            [chunk.locator_schema_version for chunk in chunks],
        ]
        collection.upsert(payload)
        collection.flush()

    def close_chunks(
        self, generation_id: str, chunk_ids: tuple[str, ...], valid_to_revision: int
    ) -> None:
        if not chunk_ids or not self._utility.has_collection(generation_id, using=self._alias):
            return
        collection = self._collection_type(generation_id, using=self._alias)
        collection.load()
        quoted = ",".join(f'"{chunk_id}"' for chunk_id in chunk_ids)
        rows = collection.query(
            expr=f"chunk_id in [{quoted}]",
            output_fields=[
                "chunk_id",
                "source_id",
                "source_version_id",
                "snapshot_id",
                "parent_id",
                "locator",
                "content_hash",
                "bm25_text",
                "dense_vector",
                "valid_from_revision",
                "parent_text",
                "locator_schema_version",
            ],
        )
        updated = tuple(
            IndexChunk(
                str(row.get("chunk_id")),
                str(row.get("source_id")),
                str(row.get("source_version_id")),
                str(row.get("snapshot_id")),
                str(row.get("parent_id")),
                str(row.get("locator")),
                str(row.get("content_hash")),
                str(row.get("bm25_text")),
                tuple(float(value) for value in row.get("dense_vector") or ()),
                int(row.get("valid_from_revision") or 0),
                valid_to_revision,
                str(row.get("parent_text") or ""),
                int(row.get("locator_schema_version") or 1),
            )
            for row in rows
        )
        if updated:
            self.upsert_chunks(generation_id, updated)

    def search_dense(
        self,
        generation_id: str,
        revision: int,
        vector: tuple[float, ...],
        top_k: int,
    ) -> tuple[IndexHit, ...]:
        if not self._utility.has_collection(generation_id, using=self._alias):
            raise KnowledgeError(
                KnowledgeErrorCode.PROVIDER_UNAVAILABLE,
                "Physical collection is missing",
            )
        collection = self._collection_type(generation_id, using=self._alias)
        collection.load()
        expr = (
            f"valid_from_revision <= {revision} && "
            f"(valid_to_revision == 0 || valid_to_revision > {revision})"
        )
        results = collection.search(
            data=[list(vector)],
            anns_field="dense_vector",
            param={"metric_type": "COSINE", "params": {"ef": 64}},
            limit=top_k,
            expr=expr,
            output_fields=[
                "chunk_id",
                "source_id",
                "source_version_id",
                "snapshot_id",
                "parent_id",
                "locator",
                "content_hash",
                "bm25_text",
                "parent_text",
                "locator_schema_version",
            ],
        )
        hits: list[IndexHit] = []
        for hit in results[0]:
            entity = hit.entity
            hits.append(
                IndexHit(
                    str(entity.get("chunk_id")),
                    str(entity.get("source_id")),
                    str(entity.get("source_version_id")),
                    str(entity.get("snapshot_id")),
                    str(entity.get("parent_id")),
                    str(entity.get("locator")),
                    str(entity.get("content_hash")),
                    str(entity.get("bm25_text")),
                    float(hit.score),
                    str(entity.get("parent_text") or ""),
                    int(entity.get("locator_schema_version") or 1),
                )
            )
        return tuple(hits)

    def search_bm25(
        self,
        generation_id: str,
        revision: int,
        query_text: str,
        top_k: int,
    ) -> tuple[IndexHit, ...]:
        # Sparse BM25 Function path is Milvus-version specific; approximate with
        # filtered scalar scan then local scoring so the Revision Filter still
        # runs before top-K even when the Function API is unavailable.
        if not self._utility.has_collection(generation_id, using=self._alias):
            raise KnowledgeError(
                KnowledgeErrorCode.PROVIDER_UNAVAILABLE,
                "Physical collection is missing",
            )
        collection = self._collection_type(generation_id, using=self._alias)
        collection.load()
        expr = (
            f"valid_from_revision <= {revision} && "
            f"(valid_to_revision == 0 || valid_to_revision > {revision})"
        )
        rows = collection.query(
            expr=expr,
            output_fields=[
                "chunk_id",
                "source_id",
                "source_version_id",
                "snapshot_id",
                "parent_id",
                "locator",
                "content_hash",
                "bm25_text",
                "parent_text",
                "locator_schema_version",
            ],
        )
        query = tokenize(query_text)
        scored = sorted(
            (
                (
                    bm25_score(query, str(row.get("bm25_text", ""))),
                    row,
                )
                for row in rows
            ),
            key=lambda item: item[0],
            reverse=True,
        )
        hits: list[IndexHit] = []
        for score, row in scored[:top_k]:
            if score <= 0:
                continue
            hits.append(
                IndexHit(
                    str(row.get("chunk_id")),
                    str(row.get("source_id")),
                    str(row.get("source_version_id")),
                    str(row.get("snapshot_id")),
                    str(row.get("parent_id")),
                    str(row.get("locator")),
                    str(row.get("content_hash")),
                    str(row.get("bm25_text")),
                    float(score),
                    str(row.get("parent_text") or ""),
                    int(row.get("locator_schema_version") or 1),
                )
            )
        return tuple(hits)

    def drop_generation(self, generation_id: str) -> None:
        if self._utility.has_collection(generation_id, using=self._alias):
            self._utility.drop_collection(generation_id, using=self._alias)

    def _ensure_collection(self, name: str, dimension: int) -> Any:
        from pymilvus import CollectionSchema, DataType, FieldSchema

        if self._utility.has_collection(name, using=self._alias):
            return self._collection_type(name, using=self._alias)
        fields = [
            FieldSchema(name="chunk_id", dtype=DataType.VARCHAR, is_primary=True, max_length=128),
            FieldSchema(name="source_id", dtype=DataType.VARCHAR, max_length=128),
            FieldSchema(name="source_version_id", dtype=DataType.VARCHAR, max_length=128),
            FieldSchema(name="snapshot_id", dtype=DataType.VARCHAR, max_length=128),
            FieldSchema(name="parent_id", dtype=DataType.VARCHAR, max_length=128),
            FieldSchema(name="locator", dtype=DataType.VARCHAR, max_length=512),
            FieldSchema(name="content_hash", dtype=DataType.VARCHAR, max_length=64),
            FieldSchema(name="bm25_text", dtype=DataType.VARCHAR, max_length=65535),
            FieldSchema(name="dense_vector", dtype=DataType.FLOAT_VECTOR, dim=dimension),
            FieldSchema(name="valid_from_revision", dtype=DataType.INT64),
            FieldSchema(name="valid_to_revision", dtype=DataType.INT64),
            FieldSchema(name="parent_text", dtype=DataType.VARCHAR, max_length=65535),
            FieldSchema(name="locator_schema_version", dtype=DataType.INT64),
        ]
        schema = CollectionSchema(fields, description="jdagent knowledge generation")
        collection = self._collection_type(name, schema, using=self._alias)
        collection.create_index(
            "dense_vector",
            {
                "index_type": "HNSW",
                "metric_type": "COSINE",
                "params": {"M": 16, "efConstruction": 64},
            },
        )
        return collection


def load_milvus_index(uri: str) -> KnowledgeIndex:
    return cast(KnowledgeIndex, MilvusKnowledgeIndex(uri))
