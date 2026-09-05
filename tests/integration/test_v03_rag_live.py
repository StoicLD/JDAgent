import os
from dataclasses import replace
from uuid import uuid4

import pytest

from jdagent.knowledge.index import IndexChunk
from jdagent.knowledge.milvus import load_milvus_index

pytestmark = pytest.mark.integration


def test_milvus_revision_filter_and_lifecycle_when_configured() -> None:
    if os.environ.get("JDAGENT_RUN_RAG_LIVE") != "1":
        pytest.skip("Set JDAGENT_RUN_RAG_LIVE=1 for live Milvus tests")
    uri = os.environ.get("JDAGENT_MILVUS_URI", "http://127.0.0.1:19530")
    index = load_milvus_index(uri)
    generation_id = f"jdagent_live_{uuid4().hex[:12]}"
    vector = (0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8)
    chunk = IndexChunk(
        "chunk-1",
        "src",
        "sv",
        "snap",
        "parent",
        "txt:p[0]",
        "hash",
        "leave policy fifteen days",
        vector,
        1,
        0,
        "leave policy fifteen days",
        1,
        "kb",
    )
    try:
        index.upsert_chunks(generation_id, (chunk,))
        visible = index.search_bm25(generation_id, 1, "leave policy", 5)
        assert visible
        index.close_chunks(generation_id, ("chunk-1",), 2)
        hidden = index.search_bm25(generation_id, 2, "leave policy", 5)
        assert hidden == ()
        index.close_chunks(generation_id, ("chunk-1",), 3)
        assert index.search_bm25(generation_id, 2, "leave policy", 5) == ()
        index.upsert_chunks(
            generation_id, (replace(chunk, chunk_id="chunk-2", valid_from_revision=3),)
        )
        reopened = index.search_bm25(generation_id, 3, "leave policy", 5)
        assert reopened
        assert index.search_bm25(generation_id, 2, "leave policy", 5) == ()
    finally:
        try:
            index.drop_generation(generation_id)
        finally:
            closer = getattr(index, "close", None)
            if callable(closer):
                closer()
