from pathlib import Path

import pytest

from jdagent.memory_store import MemoryStore, parse_memory_intent


def test_direct_intent_is_whole_utterance_only():
    assert parse_memory_intent("记住：测试使用 pytest") == (
        "remember",
        "workspace",
        None,
        "测试使用 pytest",
    )
    assert parse_memory_intent("请分析‘记住：测试使用 pytest’这段话") is None
    assert parse_memory_intent('"记住：测试使用 pytest"') is None
    intent = parse_memory_intent("记为我的通用偏好：回答使用中文")
    assert intent is not None and intent[1] == "user"


def test_intent_versioning_disable_suppression_and_scope(tmp_path: Path):
    store = MemoryStore(tmp_path / "memory.sqlite")
    intent = store.accept_intent(
        "记住：项目使用 pytest", workspace="w", session="s", turn="t", event="e"
    )
    assert intent is not None
    item = store.apply_intent(intent, workspace="w", session="s", turn="t", write_allowed=True)
    assert (
        store.apply_intent(intent, workspace="w", session="s", turn="t", write_allowed=True) == item
    )
    assert not store.recall("elsewhere")
    disable = store.accept_intent(
        f"忘记本项目记忆 {item.memory_id}", workspace="w", session="s", turn="t2", event="e2"
    )
    assert disable is not None
    with pytest.raises(PermissionError):
        store.apply_intent(disable, workspace="w", session="s", turn="t2", write_allowed=False)
    changed = store.apply_intent(disable, workspace="w", session="s", turn="t2", write_allowed=True)
    assert changed.state == "disabled" and changed.version == 2
    assert not store.recall("w")
    candidate = store.publish_candidate("w", "We use pytest", source="s:1:abc")
    assert candidate.state == "needs_verification"
    assert len(store.versions(item.memory_id)) == 2


def test_lease_epoch_fences_old_worker_and_cost_receipts_are_independent(tmp_path: Path):
    store = MemoryStore(tmp_path / "memory.sqlite")
    job = store.schedule("w", "s", 20)
    assert store.schedule("w", "s", 20) == job
    first = store.claim("w", "owner1", now=1, ttl=5)
    assert first is not None
    second = store.claim("w", "owner2", now=7, ttl=5)
    assert second is not None and second.epoch > first.epoch
    store.reserve("request", "w", "maintenance", 100, 10, input_limit=500, output_limit=100)
    with pytest.raises(ValueError, match="lease"):
        store.finish(first, now=8)
    store.settle("request", 42, 3)
    store.settle("request", 42, 3)
    assert store.costs("w") == (42, 3)
    store.finish(second, now=8)


def test_stale_intent_cannot_overwrite_user_correction(tmp_path: Path):
    store = MemoryStore(tmp_path / "memory.sqlite")
    i = store.accept_intent("记住：旧值", workspace="w", session="s", turn="t", event="1")
    assert i
    m = store.apply_intent(i, workspace="w", session="s", turn="t", write_allowed=True)
    first = store.accept_intent(
        f"把本项目记忆 {m.memory_id} 更正为：新值", workspace="w", session="s", turn="t2", event="2"
    )
    stale = store.accept_intent(
        f"忘记本项目记忆 {m.memory_id}", workspace="w", session="s", turn="t3", event="3"
    )
    assert first and stale
    store.apply_intent(first, workspace="w", session="s", turn="t2", write_allowed=True)
    with pytest.raises(ValueError, match="version"):
        store.apply_intent(stale, workspace="w", session="s", turn="t3", write_allowed=True)
