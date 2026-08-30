from pathlib import Path

import pytest

from jdagent.knowledge.catalog import KnowledgeCatalog
from jdagent.knowledge.clock import FakeClock
from jdagent.knowledge.errors import KnowledgeError, KnowledgeErrorCode
from jdagent.knowledge.types import EmbeddingProfile, RetrievalProfile


def _catalog(tmp_path: Path, clock: FakeClock | None = None) -> KnowledgeCatalog:
    return KnowledgeCatalog(
        tmp_path / "catalog.sqlite",
        tmp_path / "backups",
        clock=clock or FakeClock(),
    )


def test_register_list_and_name_conflict(tmp_path: Path) -> None:
    with _catalog(tmp_path) as catalog:
        first = catalog.register_connection(name="local", endpoint="http://127.0.0.1:19530")
        listed = catalog.list_connections()
        assert [item.connection_id for item in listed] == [first.connection_id]
        assert listed[0].credential_ref is None
        with pytest.raises(KnowledgeError) as error:
            catalog.register_connection(name="local")
        assert error.value.code is KnowledgeErrorCode.NAME_CONFLICT


def test_connection_credential_ref_is_redacted_and_validated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _catalog(tmp_path) as catalog:
        with pytest.raises(KnowledgeError) as error:
            catalog.register_connection(name="bad", credential_ref="super-secret")
        assert error.value.code is KnowledgeErrorCode.INVALID_CREDENTIAL_REF
        record = catalog.register_connection(
            name="keyed",
            credential_ref="env:JDAGENT_TEST_KEY",
        )
        assert record.credential_ref is not None
        assert record.credential_ref.redacted() == "env:JDAGENT_TEST_KEY"
        with pytest.raises(KnowledgeError) as missing:
            catalog.test_connection(record.connection_id)
        assert missing.value.code is KnowledgeErrorCode.ACCESS_DENIED
        monkeypatch.setenv("JDAGENT_TEST_KEY", "must-not-appear")
        listed = catalog.test_connection(record.connection_id)
        assert "must-not-appear" not in str(listed)
        assert listed["credential_ref"] == "env:JDAGENT_TEST_KEY"


def test_knowledge_base_and_workspace_bindings_are_isolated(tmp_path: Path) -> None:
    with _catalog(tmp_path) as catalog:
        connection = catalog.register_connection(name="local")
        hr = catalog.create_knowledge_base(connection_id=connection.connection_id, name="hr")
        with pytest.raises(KnowledgeError) as error:
            catalog.create_knowledge_base(connection_id=connection.connection_id, name="hr")
        assert error.value.code is KnowledgeErrorCode.NAME_CONFLICT
        first = catalog.bind(
            workspace_identity="ws-a",
            connection_id=connection.connection_id,
            knowledge_base_id=hr.knowledge_base_id,
        )
        catalog.bind(
            workspace_identity="ws-b",
            connection_id=connection.connection_id,
            knowledge_base_id=hr.knowledge_base_id,
        )
        assert [item.binding_id for item in catalog.list_bindings("ws-a")] == [first.binding_id]
        assert catalog.list_bindings("ws-a")[0].knowledge_base_id == hr.knowledge_base_id
        assert len(catalog.list_bindings("ws-b")) == 1
        with pytest.raises(KnowledgeError) as conflict:
            catalog.bind(
                workspace_identity="ws-a",
                connection_id=connection.connection_id,
                knowledge_base_id=hr.knowledge_base_id,
            )
        assert conflict.value.code is KnowledgeErrorCode.NAME_CONFLICT


def test_destructive_commands_require_confirmation_and_block_referenced_delete(
    tmp_path: Path,
) -> None:
    with _catalog(tmp_path) as catalog:
        connection = catalog.register_connection(name="local")
        kb = catalog.create_knowledge_base(connection_id=connection.connection_id, name="hr")
        catalog.bind(
            workspace_identity="ws-a",
            connection_id=connection.connection_id,
            knowledge_base_id=kb.knowledge_base_id,
        )
        with pytest.raises(KnowledgeError) as confirm:
            catalog.remove_connection(connection.connection_id, confirmed=False)
        assert confirm.value.code is KnowledgeErrorCode.CONFIRMATION_REQUIRED
        with pytest.raises(KnowledgeError) as referenced:
            catalog.delete_knowledge_base(kb.knowledge_base_id, confirmed=True)
        assert referenced.value.code is KnowledgeErrorCode.STILL_REFERENCED
        status = catalog.knowledge_base_status(kb.knowledge_base_id)
        assert status["binding_count"] == 1
        assert status["current_revision"] == 0
        assert status["embedding_fingerprint"]
        catalog.unbind(catalog.list_bindings("ws-a")[0].binding_id, confirmed=True)
        catalog.upsert_source(knowledge_base_id=kb.knowledge_base_id, name="policy.md")
        with pytest.raises(KnowledgeError) as sourced:
            catalog.delete_knowledge_base(kb.knowledge_base_id, confirmed=True)
        assert sourced.value.code is KnowledgeErrorCode.STILL_REFERENCED


def test_reopen_catalog_preserves_state_and_creates_daily_backup(tmp_path: Path) -> None:
    clock = FakeClock()
    sqlite_path = tmp_path / "catalog.sqlite"
    backups = tmp_path / "backups"
    with KnowledgeCatalog(sqlite_path, backups, clock=clock) as catalog:
        catalog.register_connection(name="local")
        assert list(backups.glob("catalog-*.sqlite"))
    with KnowledgeCatalog(sqlite_path, backups, clock=clock) as catalog:
        names = [item.name for item in catalog.list_connections()]
        assert names == ["local"]
        backup = catalog.create_backup(reason="manual")
        assert backup.is_file()


def test_corrupt_catalog_fails_closed(tmp_path: Path) -> None:
    sqlite_path = tmp_path / "catalog.sqlite"
    sqlite_path.write_bytes(b"this is not sqlite")
    catalog = KnowledgeCatalog(sqlite_path, tmp_path / "backups")
    with pytest.raises(KnowledgeError) as error:
        catalog.open()
    assert error.value.code is KnowledgeErrorCode.CATALOG_CORRUPT


def test_default_profiles_have_stable_fingerprints() -> None:
    first = EmbeddingProfile()
    second = EmbeddingProfile()
    assert first.fingerprint() == second.fingerprint()
    assert RetrievalProfile().fingerprint(first.fingerprint(), "mixed_zh_en_v1")
