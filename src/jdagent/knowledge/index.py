"""Knowledge index adapters with revision filtering before top-K."""

from __future__ import annotations

import json
import math
import re
import sqlite3
import threading
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Protocol


@dataclass(frozen=True, slots=True)
class IndexChunk:
    chunk_id: str
    source_id: str
    source_version_id: str
    snapshot_id: str
    parent_id: str
    locator: str
    content_hash: str
    text: str
    vector: tuple[float, ...]
    valid_from_revision: int
    valid_to_revision: int = 0
    parent_text: str = ""
    locator_schema_version: int = 1
    knowledge_base_id: str = ""


@dataclass(frozen=True, slots=True)
class IndexHit:
    chunk_id: str
    source_id: str
    source_version_id: str
    snapshot_id: str
    parent_id: str
    locator: str
    content_hash: str
    text: str
    score: float
    parent_text: str = ""
    locator_schema_version: int = 1
    knowledge_base_id: str = ""


class KnowledgeIndex(Protocol):
    def upsert_chunks(self, generation_id: str, chunks: tuple[IndexChunk, ...]) -> None: ...

    def close_chunks(
        self, generation_id: str, chunk_ids: tuple[str, ...], valid_to_revision: int
    ) -> None: ...

    def search_dense(
        self,
        generation_id: str,
        revision: int,
        vector: tuple[float, ...],
        top_k: int,
    ) -> tuple[IndexHit, ...]: ...

    def search_bm25(
        self,
        generation_id: str,
        revision: int,
        query_text: str,
        top_k: int,
    ) -> tuple[IndexHit, ...]: ...

    def drop_generation(self, generation_id: str) -> None: ...


_TOKEN = re.compile(r"[A-Za-z0-9_]+|[\u4e00-\u9fff]")


def tokenize(text: str) -> tuple[str, ...]:
    return tuple(token.lower() for token in _TOKEN.findall(text))


def _visible(chunk: IndexChunk, revision: int) -> bool:
    if chunk.valid_from_revision > revision:
        return False
    if chunk.valid_to_revision == 0:
        return True
    return chunk.valid_to_revision > revision


def _cosine(left: tuple[float, ...], right: tuple[float, ...]) -> float:
    length = min(len(left), len(right))
    if length == 0:
        return 0.0
    dot = sum(left[index] * right[index] for index in range(length))
    norm_left = math.sqrt(sum(value * value for value in left[:length])) or 1.0
    norm_right = math.sqrt(sum(value * value for value in right[:length])) or 1.0
    return dot / (norm_left * norm_right)


def bm25_score(query: tuple[str, ...], text: str) -> float:
    tokens = tokenize(text)
    if not tokens:
        return 0.0
    return float(sum(tokens.count(term) for term in query))


def _hits(chunks: list[IndexChunk], scores: list[float], top_k: int) -> tuple[IndexHit, ...]:
    ranked = sorted(zip(chunks, scores, strict=True), key=lambda item: item[1], reverse=True)
    selected = ranked[:top_k]
    return tuple(
        IndexHit(
            chunk.chunk_id,
            chunk.source_id,
            chunk.source_version_id,
            chunk.snapshot_id,
            chunk.parent_id,
            chunk.locator,
            chunk.content_hash,
            chunk.text,
            score,
            chunk.parent_text,
            chunk.locator_schema_version,
            chunk.knowledge_base_id,
        )
        for chunk, score in selected
        if score > 0
    )


class InMemoryKnowledgeIndex:
    """Process-local index used by deterministic tests."""

    def __init__(self) -> None:
        self._chunks: dict[str, dict[str, IndexChunk]] = {}

    def upsert_chunks(self, generation_id: str, chunks: tuple[IndexChunk, ...]) -> None:
        bucket = self._chunks.setdefault(generation_id, {})
        for chunk in chunks:
            bucket[chunk.chunk_id] = chunk

    def close_chunks(
        self, generation_id: str, chunk_ids: tuple[str, ...], valid_to_revision: int
    ) -> None:
        bucket = self._chunks.get(generation_id, {})
        for chunk_id in chunk_ids:
            current = bucket.get(chunk_id)
            if current is not None:
                bucket[chunk_id] = replace(current, valid_to_revision=valid_to_revision)

    def search_dense(
        self,
        generation_id: str,
        revision: int,
        vector: tuple[float, ...],
        top_k: int,
    ) -> tuple[IndexHit, ...]:
        bucket = self._chunks.get(generation_id, {})
        visible = [chunk for chunk in bucket.values() if _visible(chunk, revision)]
        scores = [_cosine(vector, chunk.vector) for chunk in visible]
        return _hits(visible, scores, top_k)

    def search_bm25(
        self,
        generation_id: str,
        revision: int,
        query_text: str,
        top_k: int,
    ) -> tuple[IndexHit, ...]:
        query = tokenize(query_text)
        bucket = self._chunks.get(generation_id, {})
        visible = [chunk for chunk in bucket.values() if _visible(chunk, revision)]
        scores = [bm25_score(query, chunk.text) for chunk in visible]
        return _hits(visible, scores, top_k)

    def drop_generation(self, generation_id: str) -> None:
        self._chunks.pop(generation_id, None)


class FileKnowledgeIndex:
    """SQLite-backed local index used when Milvus is not configured."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._db = sqlite3.connect(self._path, check_same_thread=False)
        self._db.execute(
            """
            CREATE TABLE IF NOT EXISTS chunks (
                generation_id TEXT NOT NULL,
                chunk_id TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                PRIMARY KEY (generation_id, chunk_id)
            )
            """
        )
        self._db.commit()

    def upsert_chunks(self, generation_id: str, chunks: tuple[IndexChunk, ...]) -> None:
        with self._lock:
            for chunk in chunks:
                self._db.execute(
                    """
                    INSERT INTO chunks(generation_id, chunk_id, payload_json)
                    VALUES (?, ?, ?)
                    ON CONFLICT(generation_id, chunk_id)
                    DO UPDATE SET payload_json = excluded.payload_json
                    """,
                    (generation_id, chunk.chunk_id, _chunk_to_json(chunk)),
                )
            self._db.commit()

    def close_chunks(
        self, generation_id: str, chunk_ids: tuple[str, ...], valid_to_revision: int
    ) -> None:
        with self._lock:
            for chunk_id in chunk_ids:
                row = self._db.execute(
                    "SELECT payload_json FROM chunks WHERE generation_id = ? AND chunk_id = ?",
                    (generation_id, chunk_id),
                ).fetchone()
                if row is None:
                    continue
                chunk = replace(_chunk_from_json(row[0]), valid_to_revision=valid_to_revision)
                self._db.execute(
                    """
                    UPDATE chunks SET payload_json = ?
                    WHERE generation_id = ? AND chunk_id = ?
                    """,
                    (_chunk_to_json(chunk), generation_id, chunk_id),
                )
            self._db.commit()

    def search_dense(
        self,
        generation_id: str,
        revision: int,
        vector: tuple[float, ...],
        top_k: int,
    ) -> tuple[IndexHit, ...]:
        visible = self._visible(generation_id, revision)
        scores = [_cosine(vector, chunk.vector) for chunk in visible]
        return _hits(visible, scores, top_k)

    def search_bm25(
        self,
        generation_id: str,
        revision: int,
        query_text: str,
        top_k: int,
    ) -> tuple[IndexHit, ...]:
        query = tokenize(query_text)
        visible = self._visible(generation_id, revision)
        scores = [bm25_score(query, chunk.text) for chunk in visible]
        return _hits(visible, scores, top_k)

    def drop_generation(self, generation_id: str) -> None:
        with self._lock:
            self._db.execute("DELETE FROM chunks WHERE generation_id = ?", (generation_id,))
            self._db.commit()

    def _visible(self, generation_id: str, revision: int) -> list[IndexChunk]:
        with self._lock:
            rows = self._db.execute(
                "SELECT payload_json FROM chunks WHERE generation_id = ?",
                (generation_id,),
            ).fetchall()
        chunks = [_chunk_from_json(row[0]) for row in rows]
        return [chunk for chunk in chunks if _visible(chunk, revision)]


def _chunk_to_json(chunk: IndexChunk) -> str:
    return json.dumps(
        {
            "chunk_id": chunk.chunk_id,
            "source_id": chunk.source_id,
            "source_version_id": chunk.source_version_id,
            "snapshot_id": chunk.snapshot_id,
            "parent_id": chunk.parent_id,
            "locator": chunk.locator,
            "content_hash": chunk.content_hash,
            "text": chunk.text,
            "vector": list(chunk.vector),
            "valid_from_revision": chunk.valid_from_revision,
            "valid_to_revision": chunk.valid_to_revision,
            "parent_text": chunk.parent_text,
            "locator_schema_version": chunk.locator_schema_version,
            "knowledge_base_id": chunk.knowledge_base_id,
        },
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _chunk_from_json(raw: str) -> IndexChunk:
    data = json.loads(raw)
    vector = data["vector"]
    return IndexChunk(
        chunk_id=str(data["chunk_id"]),
        source_id=str(data["source_id"]),
        source_version_id=str(data["source_version_id"]),
        snapshot_id=str(data["snapshot_id"]),
        parent_id=str(data["parent_id"]),
        locator=str(data["locator"]),
        content_hash=str(data["content_hash"]),
        text=str(data["text"]),
        vector=tuple(float(value) for value in vector),
        valid_from_revision=int(data["valid_from_revision"]),
        valid_to_revision=int(data["valid_to_revision"]),
        parent_text=str(data.get("parent_text", "")),
        locator_schema_version=int(data.get("locator_schema_version", 1)),
        knowledge_base_id=str(data.get("knowledge_base_id", "")),
    )
