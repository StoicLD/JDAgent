import sys
from types import SimpleNamespace

import pytest

from jdagent.knowledge.milvus import MIXED_ZH_EN_V1, MilvusKnowledgeIndex


def test_mixed_zh_en_v1_uses_jieba_analyzer() -> None:
    assert MIXED_ZH_EN_V1["tokenizer"] == "jieba"
    assert MIXED_ZH_EN_V1["filter"] == ["lowercase"]


def test_milvus_close_preserves_previously_closed_revision(monkeypatch: pytest.MonkeyPatch) -> None:
    class Collection:
        payloads: list[object] = []

        def __init__(self, name: str, *, using: str) -> None:
            pass

        def load(self) -> None:
            pass

        def query(self, *, expr: str, output_fields: list[str]) -> list[dict[str, object]]:
            assert "valid_to_revision" in output_fields
            return [
                {
                    "chunk_id": "old",
                    "source_id": "source",
                    "source_version_id": "version",
                    "snapshot_id": "snapshot",
                    "parent_id": "parent",
                    "locator": "txt:p[0]",
                    "content_hash": "hash",
                    "bm25_text": "policy",
                    "dense_vector": [1.0, 0.0],
                    "valid_from_revision": 1,
                    "valid_to_revision": 2,
                }
            ]

        def upsert(self, payload: object) -> None:
            self.payloads.append(payload)

        def flush(self) -> None:
            pass

    def connect(**kwargs: object) -> None:
        pass

    def disconnect(alias: str) -> None:
        pass

    def has_collection(name: str, **kwargs: object) -> bool:
        return True

    sdk = SimpleNamespace(
        Collection=Collection,
        connections=SimpleNamespace(connect=connect, disconnect=disconnect),
        utility=SimpleNamespace(has_collection=has_collection),
        CollectionSchema=object,
        DataType=object,
        FieldSchema=object,
    )
    monkeypatch.setitem(sys.modules, "pymilvus", sdk)
    index = MilvusKnowledgeIndex("http://milvus.test")
    try:
        index.close_chunks("generation", ("old",), 4)
        assert Collection.payloads == []
    finally:
        index.close()
