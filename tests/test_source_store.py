from pathlib import Path

import pytest

from jdagent.knowledge.errors import KnowledgeError, KnowledgeErrorCode
from jdagent.knowledge.store import MAX_RAW_BYTES, ContentAddressedStore, content_digest


def test_store_is_idempotent_and_verifies_hash(tmp_path: Path) -> None:
    store = ContentAddressedStore(tmp_path / "objects")
    payload = b"hello-source"
    first = store.put(payload)
    second = store.put(payload)
    assert first == second == content_digest(payload)
    assert store.get(first) == payload


def test_store_rejects_oversized_and_corrupt_objects(tmp_path: Path) -> None:
    store = ContentAddressedStore(tmp_path / "objects")
    with pytest.raises(KnowledgeError) as oversized:
        store.put(b"x" * (MAX_RAW_BYTES + 1))
    assert oversized.value.code is KnowledgeErrorCode.SOURCE_TOO_LARGE
    digest = store.put(b"ok")
    path = store.path_for(digest)
    path.write_bytes(b"tampered")
    with pytest.raises(KnowledgeError) as corrupt:
        store.get(digest)
    assert corrupt.value.code is KnowledgeErrorCode.SOURCE_CORRUPT
