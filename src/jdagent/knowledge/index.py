"""定义知识索引及本地实现，在选择排名靠前的片段前筛选可见版本。"""

from __future__ import annotations

import json
import math
import re
import sqlite3
import threading
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Protocol

from jdagent.knowledge.errors import KnowledgeError, KnowledgeErrorCode


@dataclass(frozen=True, slots=True)
class IndexChunk:
    """索引中的一个子片段，关联父段、来源版本、向量和可见的修订区间。"""

    chunk_id: str
    source_id: str
    source_version_id: str
    snapshot_id: str
    parent_id: str
    locator: str
    content_hash: str
    text: str
    vector: tuple[float, ...]
    # 起始修订号包含在可见范围内，终止修订号不包含在内。终止值为 0 表示目前仍可见。
    valid_from_revision: int
    valid_to_revision: int = 0
    parent_text: str = ""
    locator_schema_version: int = 1
    knowledge_base_id: str = ""


@dataclass(frozen=True, slots=True)
class IndexHit:
    """单个检索命中，包含排名得分及还原来源片段、父段和引用所需的信息。"""

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
    """定义写入片段、按修订关闭可见性及检索的接口，使同一检索流程可使用不同后端。"""

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

    def get_chunks(
        self, generation_id: str, chunk_ids: tuple[str, ...]
    ) -> tuple[IndexChunk, ...]: ...


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
    """本地词项检索按查询词在片段中的出现次数累加得分；重复的查询词会重复计入。"""
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
    """在当前进程内保存片段并执行检索，用于可重复的离线验证。"""

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
            if current is not None and (
                current.valid_to_revision == 0 or current.valid_to_revision > valid_to_revision
            ):
                bucket[chunk_id] = replace(current, valid_to_revision=valid_to_revision)

    def search_dense(
        self,
        generation_id: str,
        revision: int,
        vector: tuple[float, ...],
        top_k: int,
    ) -> tuple[IndexHit, ...]:
        visible = [
            chunk for chunk in self._generation(generation_id).values() if _visible(chunk, revision)
        ]
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
        visible = [
            chunk for chunk in self._generation(generation_id).values() if _visible(chunk, revision)
        ]
        scores = [bm25_score(query, chunk.text) for chunk in visible]
        return _hits(visible, scores, top_k)

    def drop_generation(self, generation_id: str) -> None:
        self._chunks.pop(generation_id, None)

    def get_chunks(self, generation_id: str, chunk_ids: tuple[str, ...]) -> tuple[IndexChunk, ...]:
        bucket = self._chunks.get(generation_id, {})
        return tuple(bucket[chunk_id] for chunk_id in chunk_ids if chunk_id in bucket)

    def _generation(self, generation_id: str) -> dict[str, IndexChunk]:
        if generation_id not in self._chunks:
            raise KnowledgeError(
                KnowledgeErrorCode.PROVIDER_UNAVAILABLE,
                "Physical collection is missing",
            )
        return self._chunks[generation_id]


class FileKnowledgeIndex:
    """用本地 SQLite 保存和检索带修订范围的片段。"""

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

    def close(self) -> None:
        with self._lock:
            self._db.close()

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
        """为指定片段设置可见区间的终点，使已有修订仍可按原范围查询。"""
        with self._lock:
            for chunk_id in chunk_ids:
                row = self._db.execute(
                    "SELECT payload_json FROM chunks WHERE generation_id = ? AND chunk_id = ?",
                    (generation_id, chunk_id),
                ).fetchone()
                if row is None:
                    continue
                current = _chunk_from_json(row[0])
                if 0 < current.valid_to_revision <= valid_to_revision:
                    continue
                chunk = replace(current, valid_to_revision=valid_to_revision)
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

    def get_chunks(self, generation_id: str, chunk_ids: tuple[str, ...]) -> tuple[IndexChunk, ...]:
        found: list[IndexChunk] = []
        with self._lock:
            for chunk_id in chunk_ids:
                row = self._db.execute(
                    "SELECT payload_json FROM chunks WHERE generation_id = ? AND chunk_id = ?",
                    (generation_id, chunk_id),
                ).fetchone()
                if row is not None:
                    found.append(_chunk_from_json(row[0]))
        return tuple(found)

    def _visible(self, generation_id: str, revision: int) -> list[IndexChunk]:
        with self._lock:
            rows = self._db.execute(
                "SELECT payload_json FROM chunks WHERE generation_id = ?",
                (generation_id,),
            ).fetchall()
        if not rows:
            raise KnowledgeError(
                KnowledgeErrorCode.PROVIDER_UNAVAILABLE,
                "Physical collection is missing",
            )
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
