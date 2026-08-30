from pathlib import Path

import pytest

from jdagent.knowledge.errors import KnowledgeError, KnowledgeErrorCode
from jdagent.knowledge.index import IndexChunk, InMemoryKnowledgeIndex


def _chunk(chunk_id: str, text: str, revision: int, vector: tuple[float, ...]) -> IndexChunk:
    return IndexChunk(
        chunk_id=chunk_id,
        source_id="src",
        source_version_id="sv",
        snapshot_id="snap",
        parent_id="par",
        locator=f"txt:p[{chunk_id}]",
        content_hash=chunk_id,
        text=text,
        vector=vector,
        valid_from_revision=revision,
    )


def test_revision_filter_applies_before_top_k() -> None:
    index = InMemoryKnowledgeIndex()
    index.upsert_chunks(
        "gen-1",
        (
            _chunk("old", "年假 15 天", 1, (1.0, 0.0)),
            _chunk("new", "年假 20 天", 2, (0.9, 0.1)),
        ),
    )
    index.close_chunks("gen-1", ("old",), 2)
    frozen = index.search_dense("gen-1", 1, (1.0, 0.0), top_k=1)
    current = index.search_dense("gen-1", 2, (1.0, 0.0), top_k=1)
    assert frozen[0].chunk_id == "old"
    assert current[0].chunk_id == "new"
    bm25 = index.search_bm25("gen-1", 2, "年假", top_k=5)
    assert [hit.chunk_id for hit in bm25] == ["new"]
    with pytest.raises(KnowledgeError) as missing:
        index.search_dense("missing-gen", 1, (1.0, 0.0), top_k=1)
    assert missing.value.code is KnowledgeErrorCode.PROVIDER_UNAVAILABLE


def test_file_index_filters_revision_before_top_k(tmp_path: Path) -> None:
    from jdagent.knowledge.index import FileKnowledgeIndex

    index = FileKnowledgeIndex(tmp_path / "index.sqlite")
    index.upsert_chunks(
        "gen-1",
        (
            _chunk("old", "年假 15 天", 1, (1.0, 0.0)),
            _chunk("new", "年假 20 天", 2, (0.9, 0.1)),
        ),
    )
    index.close_chunks("gen-1", ("old",), 2)
    frozen = index.search_dense("gen-1", 1, (1.0, 0.0), top_k=1)
    current = index.search_dense("gen-1", 2, (1.0, 0.0), top_k=1)
    assert frozen[0].chunk_id == "old"
    assert current[0].chunk_id == "new"
    with pytest.raises(KnowledgeError) as missing:
        index.search_bm25("missing-gen", 1, "年假", top_k=1)
    assert missing.value.code is KnowledgeErrorCode.PROVIDER_UNAVAILABLE
    index.close()
