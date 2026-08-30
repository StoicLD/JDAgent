"""SQLite knowledge catalog: identities, bindings, integrity, and backups."""

from __future__ import annotations

import json
import os
import re
import sqlite3
import threading
from datetime import datetime, timedelta
from pathlib import Path
from typing import Protocol
from uuid import uuid4

from jdagent.knowledge.clock import SystemClock
from jdagent.knowledge.errors import KnowledgeError, KnowledgeErrorCode
from jdagent.knowledge.types import (
    BindingRecord,
    ConnectionRecord,
    CredentialRef,
    EmbeddingProfile,
    KnowledgeBaseRecord,
    KnowledgeBaseStatus,
    OperationKind,
    OperationRecord,
    ProviderKind,
    RetrievalProfile,
    SagaStage,
    SourceLifecycle,
    SourceRecord,
    SourceVersionRecord,
)

_CREDENTIAL_REF = re.compile(r"^(env|file):(.+)$")
_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
CATALOG_SCHEMA_VERSION = 2
LEASE_TTL_SECONDS = 300


class Clock(Protocol):
    def now(self) -> datetime: ...


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex}"


def parse_credential_ref(raw: str | None) -> CredentialRef | None:
    if raw is None or raw == "":
        return None
    match = _CREDENTIAL_REF.fullmatch(raw.strip())
    if match is None:
        raise KnowledgeError(
            KnowledgeErrorCode.INVALID_CREDENTIAL_REF,
            "credential_ref must be env:<NAME> or file:<path>",
        )
    kind, value = match.group(1), match.group(2)
    if not value.strip():
        raise KnowledgeError(
            KnowledgeErrorCode.INVALID_CREDENTIAL_REF,
            "credential_ref must include a non-empty target",
        )
    return CredentialRef(kind, value)


def _require_name(name: str, field: str) -> str:
    stripped = name.strip()
    if _NAME.fullmatch(stripped) is None:
        raise KnowledgeError(
            KnowledgeErrorCode.INVALID_ARGUMENT,
            f"{field} must be a stable identifier",
        )
    return stripped


def _parse_time(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise KnowledgeError(KnowledgeErrorCode.CATALOG_CORRUPT, "Catalog timestamp is not aware")
    return parsed


class KnowledgeCatalog:
    """Own the user-level SQLite catalog without contacting Milvus or Embedding."""

    def __init__(
        self,
        sqlite_path: Path,
        backup_directory: Path,
        *,
        clock: Clock | None = None,
    ) -> None:
        self._sqlite_path = sqlite_path
        self._backup_directory = backup_directory
        self._clock = clock or SystemClock()
        self._lock = threading.Lock()
        self._connection: sqlite3.Connection | None = None

    def open(self) -> None:
        self._sqlite_path.parent.mkdir(parents=True, exist_ok=True)
        self._backup_directory.mkdir(parents=True, exist_ok=True)
        if self._sqlite_path.exists():
            self._quick_check()
        connection = sqlite3.connect(self._sqlite_path, check_same_thread=False)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        self._connection = connection
        try:
            self._migrate()
        except Exception:
            connection.close()
            self._connection = None
            raise

    def close(self) -> None:
        if self._connection is not None:
            self._connection.close()
            self._connection = None

    def __enter__(self) -> KnowledgeCatalog:
        self.open()
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    def register_connection(
        self,
        *,
        name: str,
        endpoint: str | None = None,
        credential_ref: str | None = None,
        provider_kind: ProviderKind = ProviderKind.JDAGENT_MANAGED,
    ) -> ConnectionRecord:
        stable_name = _require_name(name, "name")
        parsed_ref = parse_credential_ref(credential_ref)
        now = self._clock.now().isoformat()
        connection_id = new_id("conn")
        with self._lock:
            db = self._db()
            try:
                db.execute(
                    """
                    INSERT INTO connections (
                        connection_id, name, provider_kind, endpoint, credential_ref,
                        config_json, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, '{}', ?, ?)
                    """,
                    (
                        connection_id,
                        stable_name,
                        provider_kind.value,
                        endpoint,
                        None if parsed_ref is None else f"{parsed_ref.kind}:{parsed_ref.value}",
                        now,
                        now,
                    ),
                )
            except sqlite3.IntegrityError as error:
                raise KnowledgeError(
                    KnowledgeErrorCode.NAME_CONFLICT,
                    f"Connection name already exists: {stable_name}",
                ) from error
            self._commit_with_backup(db)
        return self.get_connection(connection_id)

    def test_connection(self, connection_id: str) -> dict[str, str]:
        record = self.get_connection(connection_id)
        if record.credential_ref is not None and record.credential_ref.kind == "env":
            if os.environ.get(record.credential_ref.value) is None:
                raise KnowledgeError(
                    KnowledgeErrorCode.ACCESS_DENIED,
                    "Credential environment variable is not set",
                )
        if record.credential_ref is not None and record.credential_ref.kind == "file":
            path = Path(record.credential_ref.value)
            if not path.is_file():
                raise KnowledgeError(
                    KnowledgeErrorCode.ACCESS_DENIED,
                    "Credential file is not readable",
                )
        return {
            "connection_id": record.connection_id,
            "status": "ok",
            "provider_kind": record.provider_kind.value,
            "credential_ref": "none"
            if record.credential_ref is None
            else record.credential_ref.redacted(),
        }

    def list_connections(self) -> tuple[ConnectionRecord, ...]:
        with self._lock:
            rows = (
                self._db()
                .execute("SELECT * FROM connections ORDER BY name COLLATE NOCASE")
                .fetchall()
            )
        return tuple(self._connection_from_row(row) for row in rows)

    def get_connection(self, connection_id: str) -> ConnectionRecord:
        with self._lock:
            row = (
                self._db()
                .execute(
                    "SELECT * FROM connections WHERE connection_id = ?",
                    (connection_id,),
                )
                .fetchone()
            )
        if row is None:
            raise KnowledgeError(KnowledgeErrorCode.NOT_FOUND, "Connection not found")
        return self._connection_from_row(row)

    def remove_connection(self, connection_id: str, *, confirmed: bool) -> None:
        if not confirmed:
            raise KnowledgeError(
                KnowledgeErrorCode.CONFIRMATION_REQUIRED,
                "Removing a connection requires confirmation",
            )
        with self._lock:
            db = self._db()
            self._connection_from_row_or_raise(db, connection_id)
            kb_count = db.execute(
                "SELECT COUNT(*) FROM knowledge_bases WHERE connection_id = ?",
                (connection_id,),
            ).fetchone()[0]
            bind_count = db.execute(
                "SELECT COUNT(*) FROM bindings WHERE connection_id = ?",
                (connection_id,),
            ).fetchone()[0]
            if kb_count or bind_count:
                raise KnowledgeError(
                    KnowledgeErrorCode.STILL_REFERENCED,
                    "Connection is still referenced by a knowledge base or binding",
                )
            db.execute("DELETE FROM connections WHERE connection_id = ?", (connection_id,))
            self._commit_with_backup(db)

    def create_knowledge_base(
        self,
        *,
        connection_id: str,
        name: str,
        embedding_profile: EmbeddingProfile | None = None,
        retrieval_profile: RetrievalProfile | None = None,
        language_profile: str = "mixed_zh_en_v1",
    ) -> KnowledgeBaseRecord:
        stable_name = _require_name(name, "name")
        embedding = embedding_profile or EmbeddingProfile()
        retrieval = retrieval_profile or RetrievalProfile()
        now = self._clock.now().isoformat()
        knowledge_base_id = new_id("kb")
        with self._lock:
            db = self._db()
            self._connection_from_row_or_raise(db, connection_id)
            try:
                db.execute(
                    """
                    INSERT INTO knowledge_bases (
                        knowledge_base_id, connection_id, name, status,
                        embedding_profile_json, retrieval_profile_json, language_profile,
                        current_generation_id, current_revision, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, NULL, 0, ?, ?)
                    """,
                    (
                        knowledge_base_id,
                        connection_id,
                        stable_name,
                        KnowledgeBaseStatus.ACTIVE.value,
                        json.dumps(embedding.to_json(), ensure_ascii=False, sort_keys=True),
                        json.dumps(retrieval.to_json(), ensure_ascii=False, sort_keys=True),
                        language_profile,
                        now,
                        now,
                    ),
                )
            except sqlite3.IntegrityError as error:
                raise KnowledgeError(
                    KnowledgeErrorCode.NAME_CONFLICT,
                    f"Knowledge base name already exists: {stable_name}",
                ) from error
            self._commit_with_backup(db)
        return self.get_knowledge_base(knowledge_base_id)

    def list_knowledge_bases(
        self, connection_id: str | None = None
    ) -> tuple[KnowledgeBaseRecord, ...]:
        with self._lock:
            db = self._db()
            if connection_id is None:
                rows = db.execute(
                    "SELECT * FROM knowledge_bases ORDER BY name COLLATE NOCASE"
                ).fetchall()
            else:
                rows = db.execute(
                    """
                    SELECT * FROM knowledge_bases
                    WHERE connection_id = ? ORDER BY name COLLATE NOCASE
                    """,
                    (connection_id,),
                ).fetchall()
        return tuple(self._knowledge_base_from_row(row) for row in rows)

    def get_knowledge_base(self, knowledge_base_id: str) -> KnowledgeBaseRecord:
        with self._lock:
            row = (
                self._db()
                .execute(
                    "SELECT * FROM knowledge_bases WHERE knowledge_base_id = ?",
                    (knowledge_base_id,),
                )
                .fetchone()
            )
        if row is None:
            raise KnowledgeError(KnowledgeErrorCode.NOT_FOUND, "Knowledge base not found")
        return self._knowledge_base_from_row(row)

    def knowledge_base_status(self, knowledge_base_id: str) -> dict[str, object]:
        record = self.get_knowledge_base(knowledge_base_id)
        with self._lock:
            binding_count = (
                self._db()
                .execute(
                    "SELECT COUNT(*) FROM bindings WHERE knowledge_base_id = ?",
                    (knowledge_base_id,),
                )
                .fetchone()[0]
            )
        return {
            "knowledge_base_id": record.knowledge_base_id,
            "connection_id": record.connection_id,
            "name": record.name,
            "status": record.status.value,
            "language_profile": record.language_profile,
            "current_generation_id": record.current_generation_id,
            "current_revision": record.current_revision,
            "embedding_fingerprint": record.embedding_profile.fingerprint(),
            "binding_count": binding_count,
        }

    def delete_knowledge_base(self, knowledge_base_id: str, *, confirmed: bool) -> None:
        if not confirmed:
            raise KnowledgeError(
                KnowledgeErrorCode.CONFIRMATION_REQUIRED,
                "Deleting a knowledge base requires confirmation",
            )
        with self._lock:
            db = self._db()
            row = db.execute(
                "SELECT * FROM knowledge_bases WHERE knowledge_base_id = ?",
                (knowledge_base_id,),
            ).fetchone()
            if row is None:
                raise KnowledgeError(KnowledgeErrorCode.NOT_FOUND, "Knowledge base not found")
            bind_count = db.execute(
                "SELECT COUNT(*) FROM bindings WHERE knowledge_base_id = ?",
                (knowledge_base_id,),
            ).fetchone()[0]
            if bind_count:
                raise KnowledgeError(
                    KnowledgeErrorCode.STILL_REFERENCED,
                    "Knowledge base is still referenced by a workspace binding",
                )
            db.execute(
                "DELETE FROM knowledge_bases WHERE knowledge_base_id = ?",
                (knowledge_base_id,),
            )
            self._commit_with_backup(db)

    def bind(
        self,
        *,
        workspace_identity: str,
        connection_id: str,
        knowledge_base_id: str,
    ) -> BindingRecord:
        if not workspace_identity.strip():
            raise KnowledgeError(
                KnowledgeErrorCode.INVALID_ARGUMENT,
                "workspace_identity must not be empty",
            )
        binding_id = new_id("bind")
        now = self._clock.now().isoformat()
        with self._lock:
            db = self._db()
            self._connection_from_row_or_raise(db, connection_id)
            kb = db.execute(
                "SELECT * FROM knowledge_bases WHERE knowledge_base_id = ?",
                (knowledge_base_id,),
            ).fetchone()
            if kb is None:
                raise KnowledgeError(
                    KnowledgeErrorCode.NOT_FOUND,
                    "Knowledge base not found",
                )
            if kb["connection_id"] != connection_id:
                raise KnowledgeError(
                    KnowledgeErrorCode.INVALID_ARGUMENT,
                    "Knowledge base does not belong to the given connection",
                )
            try:
                db.execute(
                    """
                    INSERT INTO bindings (
                        binding_id, workspace_identity, connection_id, knowledge_base_id,
                        created_at
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    (binding_id, workspace_identity, connection_id, knowledge_base_id, now),
                )
            except sqlite3.IntegrityError as error:
                raise KnowledgeError(
                    KnowledgeErrorCode.NAME_CONFLICT,
                    "Workspace is already bound to this knowledge base",
                ) from error
            self._commit_with_backup(db)
        return self.get_binding(binding_id)

    def unbind(self, binding_id: str, *, confirmed: bool) -> None:
        if not confirmed:
            raise KnowledgeError(
                KnowledgeErrorCode.CONFIRMATION_REQUIRED,
                "Removing a binding requires confirmation",
            )
        with self._lock:
            db = self._db()
            row = db.execute(
                "SELECT binding_id FROM bindings WHERE binding_id = ?",
                (binding_id,),
            ).fetchone()
            if row is None:
                raise KnowledgeError(KnowledgeErrorCode.NOT_FOUND, "Binding not found")
            db.execute("DELETE FROM bindings WHERE binding_id = ?", (binding_id,))
            self._commit_with_backup(db)

    def list_bindings(self, workspace_identity: str) -> tuple[BindingRecord, ...]:
        with self._lock:
            rows = (
                self._db()
                .execute(
                    """
                SELECT * FROM bindings
                WHERE workspace_identity = ?
                ORDER BY created_at
                """,
                    (workspace_identity,),
                )
                .fetchall()
            )
        return tuple(self._binding_from_row(row) for row in rows)

    def get_binding(self, binding_id: str) -> BindingRecord:
        with self._lock:
            row = (
                self._db()
                .execute(
                    "SELECT * FROM bindings WHERE binding_id = ?",
                    (binding_id,),
                )
                .fetchone()
            )
        if row is None:
            raise KnowledgeError(KnowledgeErrorCode.NOT_FOUND, "Binding not found")
        return self._binding_from_row(row)

    def acquire_lease(self, operation_id: str, owner: str) -> None:
        now = self._clock.now()
        with self._lock:
            db = self._db()
            row = db.execute("SELECT * FROM leases WHERE lease_id = 'global'").fetchone()
            if row is not None:
                expires = _parse_time(row["expires_at"])
                if expires > now and row["operation_id"] != operation_id:
                    raise KnowledgeError(
                        KnowledgeErrorCode.KNOWLEDGE_BUSY,
                        "Knowledge catalog is busy",
                    )
            expires_at = (now + timedelta(seconds=LEASE_TTL_SECONDS)).isoformat()
            db.execute(
                """
                INSERT INTO leases(
                    lease_id, operation_id, owner, acquired_at, heartbeat_at, expires_at
                ) VALUES ('global', ?, ?, ?, ?, ?)
                ON CONFLICT(lease_id) DO UPDATE SET
                    operation_id = excluded.operation_id,
                    owner = excluded.owner,
                    heartbeat_at = excluded.heartbeat_at,
                    expires_at = excluded.expires_at
                """,
                (operation_id, owner, now.isoformat(), now.isoformat(), expires_at),
            )
            self._commit_with_backup(db)

    def release_lease(self, operation_id: str) -> None:
        with self._lock:
            db = self._db()
            db.execute(
                "DELETE FROM leases WHERE lease_id = 'global' AND operation_id = ?",
                (operation_id,),
            )
            self._commit_with_backup(db)

    def record_operation(
        self,
        *,
        operation_id: str,
        kind: OperationKind,
        knowledge_base_id: str,
        source_id: str | None,
        stage: SagaStage,
        payload_json: str = "{}",
        error_code: str | None = None,
        error_message: str | None = None,
    ) -> OperationRecord:
        now = self._clock.now().isoformat()
        with self._lock:
            db = self._db()
            existing = db.execute(
                "SELECT created_at FROM operations WHERE operation_id = ?",
                (operation_id,),
            ).fetchone()
            created = existing["created_at"] if existing is not None else now
            db.execute(
                """
                INSERT INTO operations (
                    operation_id, kind, saga_stage, knowledge_base_id, source_id,
                    payload_json, error_code, error_message, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(operation_id) DO UPDATE SET
                    saga_stage = excluded.saga_stage,
                    source_id = excluded.source_id,
                    payload_json = excluded.payload_json,
                    error_code = excluded.error_code,
                    error_message = excluded.error_message,
                    updated_at = excluded.updated_at
                """,
                (
                    operation_id,
                    kind.value,
                    stage.value,
                    knowledge_base_id,
                    source_id,
                    payload_json,
                    error_code,
                    error_message,
                    created,
                    now,
                ),
            )
            self._commit_with_backup(db)
        return self.get_operation(operation_id)

    def get_operation(self, operation_id: str) -> OperationRecord:
        with self._lock:
            row = (
                self._db()
                .execute(
                    "SELECT * FROM operations WHERE operation_id = ?",
                    (operation_id,),
                )
                .fetchone()
            )
        if row is None:
            raise KnowledgeError(KnowledgeErrorCode.NOT_FOUND, "Operation not found")
        return OperationRecord(
            operation_id=row["operation_id"],
            kind=OperationKind(row["kind"]),
            saga_stage=row["saga_stage"],
            knowledge_base_id=row["knowledge_base_id"],
            source_id=row["source_id"],
            payload_json=row["payload_json"],
            error_code=row["error_code"],
            created_at=_parse_time(row["created_at"]),
            updated_at=_parse_time(row["updated_at"]),
        )

    def expire_stale_leases(self) -> int:
        now = self._clock.now()
        with self._lock:
            db = self._db()
            row = db.execute("SELECT * FROM leases WHERE lease_id = 'global'").fetchone()
            if row is None:
                return 0
            if _parse_time(row["expires_at"]) > now:
                return 0
            db.execute("DELETE FROM leases WHERE lease_id = 'global'")
            self._commit_with_backup(db)
        return 1

    def upsert_source(
        self,
        *,
        knowledge_base_id: str,
        name: str,
        source_id: str | None = None,
        lifecycle: SourceLifecycle = SourceLifecycle.ACTIVE,
        current_version_id: str | None = None,
    ) -> SourceRecord:
        now = self._clock.now().isoformat()
        identifier = source_id or new_id("src")
        with self._lock:
            db = self._db()
            existing = db.execute(
                "SELECT * FROM sources WHERE knowledge_base_id = ? AND name = ?",
                (knowledge_base_id, name),
            ).fetchone()
            if existing is not None:
                identifier = existing["source_id"]
                db.execute(
                    """
                    UPDATE sources SET lifecycle = ?, current_version_id = ?, updated_at = ?
                    WHERE source_id = ?
                    """,
                    (lifecycle.value, current_version_id, now, identifier),
                )
            else:
                db.execute(
                    """
                    INSERT INTO sources (
                        source_id, knowledge_base_id, name, lifecycle, current_version_id,
                        created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        identifier,
                        knowledge_base_id,
                        name,
                        lifecycle.value,
                        current_version_id,
                        now,
                        now,
                    ),
                )
            self._commit_with_backup(db)
        return self.get_source(identifier)

    def get_source(self, source_id: str) -> SourceRecord:
        with self._lock:
            row = (
                self._db()
                .execute(
                    "SELECT * FROM sources WHERE source_id = ?",
                    (source_id,),
                )
                .fetchone()
            )
        if row is None:
            raise KnowledgeError(KnowledgeErrorCode.NOT_FOUND, "Source not found")
        return SourceRecord(
            source_id=row["source_id"],
            knowledge_base_id=row["knowledge_base_id"],
            name=row["name"],
            lifecycle=SourceLifecycle(row["lifecycle"]),
            current_version_id=row["current_version_id"],
            created_at=_parse_time(row["created_at"]),
            updated_at=_parse_time(row["updated_at"]),
        )

    def get_source_by_name(self, knowledge_base_id: str, name: str) -> SourceRecord | None:
        with self._lock:
            row = (
                self._db()
                .execute(
                    "SELECT * FROM sources WHERE knowledge_base_id = ? AND name = ?",
                    (knowledge_base_id, name),
                )
                .fetchone()
            )
        if row is None:
            return None
        return self.get_source(row["source_id"])

    def list_sources(self, knowledge_base_id: str) -> tuple[SourceRecord, ...]:
        with self._lock:
            rows = (
                self._db()
                .execute(
                    "SELECT source_id FROM sources WHERE knowledge_base_id = ? ORDER BY name",
                    (knowledge_base_id,),
                )
                .fetchall()
            )
        return tuple(self.get_source(row["source_id"]) for row in rows)

    def get_source_version(self, source_version_id: str) -> SourceVersionRecord:
        with self._lock:
            row = (
                self._db()
                .execute(
                    "SELECT * FROM source_versions WHERE source_version_id = ?",
                    (source_version_id,),
                )
                .fetchone()
            )
        if row is None:
            raise KnowledgeError(KnowledgeErrorCode.NOT_FOUND, "Source version not found")
        return SourceVersionRecord(
            source_version_id=row["source_version_id"],
            source_id=row["source_id"],
            raw_hash=row["raw_hash"],
            encoding=str(row["encoding"] or ""),
            encoding_method=str(row["encoding_method"] or ""),
            snapshot_hash=str(row["snapshot_hash"] or ""),
            parser_profile=str(row["parser_profile"] or ""),
            created_at=_parse_time(row["created_at"]),
        )

    def add_source_version(
        self,
        *,
        source_id: str,
        raw_hash: str,
        encoding: str,
        encoding_method: str,
        snapshot_hash: str,
        parser_profile: str,
    ) -> str:
        version_id = new_id("sv")
        now = self._clock.now().isoformat()
        with self._lock:
            db = self._db()
            db.execute(
                """
                INSERT INTO source_versions (
                    source_version_id, source_id, raw_hash, encoding, encoding_method,
                    snapshot_hash, parser_profile, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    version_id,
                    source_id,
                    raw_hash,
                    encoding,
                    encoding_method,
                    snapshot_hash,
                    parser_profile,
                    now,
                ),
            )
            self._commit_with_backup(db)
        return version_id

    def set_source_lifecycle(
        self, source_id: str, lifecycle: SourceLifecycle, *, current_version_id: str | None = None
    ) -> None:
        now = self._clock.now().isoformat()
        with self._lock:
            db = self._db()
            if current_version_id is None:
                db.execute(
                    "UPDATE sources SET lifecycle = ?, updated_at = ? WHERE source_id = ?",
                    (lifecycle.value, now, source_id),
                )
            else:
                db.execute(
                    """
                    UPDATE sources SET lifecycle = ?, current_version_id = ?, updated_at = ?
                    WHERE source_id = ?
                    """,
                    (lifecycle.value, current_version_id, now, source_id),
                )
            self._commit_with_backup(db)

    def activate_revision(self, knowledge_base_id: str, generation_id: str, revision: int) -> None:
        now = self._clock.now().isoformat()
        with self._lock:
            db = self._db()
            db.execute(
                """
                UPDATE knowledge_bases
                SET current_generation_id = ?, current_revision = ?, updated_at = ?
                WHERE knowledge_base_id = ?
                """,
                (generation_id, revision, now, knowledge_base_id),
            )
            self._commit_with_backup(db)

    def remember_chunks(
        self, generation_id: str, source_id: str, chunk_ids: tuple[str, ...]
    ) -> None:
        with self._lock:
            db = self._db()
            for chunk_id in chunk_ids:
                db.execute(
                    """
                    INSERT OR REPLACE INTO generation_chunks(generation_id, chunk_id, source_id)
                    VALUES (?, ?, ?)
                    """,
                    (generation_id, chunk_id, source_id),
                )
            self._commit_with_backup(db)

    def chunks_for_source(self, generation_id: str, source_id: str) -> tuple[str, ...]:
        with self._lock:
            rows = (
                self._db()
                .execute(
                    """
                SELECT chunk_id FROM generation_chunks
                WHERE generation_id = ? AND source_id = ?
                """,
                    (generation_id, source_id),
                )
                .fetchall()
            )
        return tuple(row["chunk_id"] for row in rows)

    def create_backup(self, *, reason: str = "manual") -> Path:
        with self._lock:
            return self._backup_locked(self._db(), reason=reason)

    def _db(self) -> sqlite3.Connection:
        if self._connection is None:
            raise KnowledgeError(
                KnowledgeErrorCode.INVALID_ARGUMENT,
                "Knowledge catalog is not open",
            )
        return self._connection

    def _quick_check(self) -> None:
        try:
            probe = sqlite3.connect(self._sqlite_path)
        except sqlite3.Error as error:
            raise KnowledgeError(
                KnowledgeErrorCode.CATALOG_CORRUPT,
                "Knowledge catalog failed integrity check",
            ) from error
        try:
            row = probe.execute("PRAGMA quick_check").fetchone()
        except sqlite3.Error as error:
            raise KnowledgeError(
                KnowledgeErrorCode.CATALOG_CORRUPT,
                "Knowledge catalog failed integrity check",
            ) from error
        finally:
            probe.close()
        if row is None or str(row[0]) != "ok":
            raise KnowledgeError(
                KnowledgeErrorCode.CATALOG_CORRUPT,
                "Knowledge catalog failed integrity check",
            )

    def _migrate(self) -> None:
        db = self._db()
        db.execute(
            "CREATE TABLE IF NOT EXISTS catalog_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
        )
        row = db.execute("SELECT value FROM catalog_meta WHERE key = 'schema_version'").fetchone()
        current = int(row["value"]) if row is not None else 0
        if current > CATALOG_SCHEMA_VERSION:
            raise KnowledgeError(
                KnowledgeErrorCode.CATALOG_CORRUPT,
                "Knowledge catalog schema is newer than this JDAgent build",
            )
        if current != 0 and current < CATALOG_SCHEMA_VERSION:
            self._backup_locked(db, reason="migrate")
            integrity = db.execute("PRAGMA integrity_check").fetchone()
            if integrity is None or str(integrity[0]) != "ok":
                raise KnowledgeError(
                    KnowledgeErrorCode.CATALOG_CORRUPT,
                    "Knowledge catalog failed integrity check before migration",
                )
        if current < 1:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS connections (
                    connection_id TEXT PRIMARY KEY,
                    name TEXT NOT NULL UNIQUE,
                    provider_kind TEXT NOT NULL,
                    endpoint TEXT,
                    credential_ref TEXT,
                    config_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS knowledge_bases (
                    knowledge_base_id TEXT PRIMARY KEY,
                    connection_id TEXT NOT NULL REFERENCES connections(connection_id),
                    name TEXT NOT NULL,
                    status TEXT NOT NULL,
                    embedding_profile_json TEXT NOT NULL,
                    retrieval_profile_json TEXT NOT NULL,
                    language_profile TEXT NOT NULL,
                    current_generation_id TEXT,
                    current_revision INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE (connection_id, name)
                );
                CREATE TABLE IF NOT EXISTS bindings (
                    binding_id TEXT PRIMARY KEY,
                    workspace_identity TEXT NOT NULL,
                    connection_id TEXT NOT NULL REFERENCES connections(connection_id),
                    knowledge_base_id TEXT NOT NULL REFERENCES knowledge_bases(knowledge_base_id),
                    created_at TEXT NOT NULL,
                    UNIQUE (workspace_identity, knowledge_base_id)
                );
                """
            )
        if current < 2:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS sources (
                    source_id TEXT PRIMARY KEY,
                    knowledge_base_id TEXT NOT NULL REFERENCES knowledge_bases(knowledge_base_id),
                    name TEXT NOT NULL,
                    lifecycle TEXT NOT NULL,
                    current_version_id TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE (knowledge_base_id, name)
                );
                CREATE TABLE IF NOT EXISTS source_versions (
                    source_version_id TEXT PRIMARY KEY,
                    source_id TEXT NOT NULL REFERENCES sources(source_id),
                    raw_hash TEXT NOT NULL,
                    encoding TEXT,
                    encoding_method TEXT,
                    snapshot_hash TEXT,
                    parser_profile TEXT,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS operations (
                    operation_id TEXT PRIMARY KEY,
                    kind TEXT NOT NULL,
                    saga_stage TEXT NOT NULL,
                    knowledge_base_id TEXT NOT NULL,
                    source_id TEXT,
                    payload_json TEXT NOT NULL,
                    error_code TEXT,
                    error_message TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS leases (
                    lease_id TEXT PRIMARY KEY,
                    operation_id TEXT NOT NULL,
                    owner TEXT NOT NULL,
                    acquired_at TEXT NOT NULL,
                    heartbeat_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS generation_chunks (
                    generation_id TEXT NOT NULL,
                    chunk_id TEXT NOT NULL,
                    source_id TEXT NOT NULL,
                    PRIMARY KEY (generation_id, chunk_id)
                );
                """
            )
        db.execute(
            """
            INSERT INTO catalog_meta(key, value) VALUES ('schema_version', ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """,
            (str(CATALOG_SCHEMA_VERSION),),
        )
        db.commit()

    def _commit_with_backup(self, db: sqlite3.Connection) -> None:
        db.commit()
        today = self._clock.now().date().isoformat()
        last = db.execute(
            "SELECT value FROM catalog_meta WHERE key = 'last_backup_date'"
        ).fetchone()
        if last is None or last["value"] != today:
            self._backup_locked(db, reason="daily")
            self._prune_backups()

    def _backup_locked(self, db: sqlite3.Connection, *, reason: str) -> Path:
        stamp = self._clock.now().strftime("%Y%m%dT%H%M%SZ")
        target = self._backup_directory / f"catalog-{stamp}-{reason}.sqlite"
        destination = sqlite3.connect(target)
        try:
            db.backup(destination)
            destination.execute("PRAGMA integrity_check")
            check = destination.execute("PRAGMA integrity_check").fetchone()
            if check is None or str(check[0]) != "ok":
                destination.close()
                target.unlink(missing_ok=True)
                raise KnowledgeError(
                    KnowledgeErrorCode.CATALOG_CORRUPT,
                    "Knowledge catalog backup failed integrity check",
                )
        finally:
            destination.close()
        db.execute(
            """
            INSERT INTO catalog_meta(key, value) VALUES ('last_backup_date', ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """,
            (self._clock.now().date().isoformat(),),
        )
        db.commit()
        return target

    def _prune_backups(self) -> None:
        backups = sorted(self._backup_directory.glob("catalog-*.sqlite"))
        for stale in backups[:-7]:
            stale.unlink(missing_ok=True)

    def _connection_from_row_or_raise(
        self, db: sqlite3.Connection, connection_id: str
    ) -> sqlite3.Row:
        row = db.execute(
            "SELECT * FROM connections WHERE connection_id = ?",
            (connection_id,),
        ).fetchone()
        if row is None:
            raise KnowledgeError(KnowledgeErrorCode.NOT_FOUND, "Connection not found")
        return row

    @staticmethod
    def _connection_from_row(row: sqlite3.Row) -> ConnectionRecord:
        raw_ref = row["credential_ref"]
        return ConnectionRecord(
            connection_id=row["connection_id"],
            name=row["name"],
            provider_kind=ProviderKind(row["provider_kind"]),
            endpoint=row["endpoint"],
            credential_ref=parse_credential_ref(raw_ref) if raw_ref else None,
            created_at=_parse_time(row["created_at"]),
            updated_at=_parse_time(row["updated_at"]),
        )

    @staticmethod
    def _knowledge_base_from_row(row: sqlite3.Row) -> KnowledgeBaseRecord:
        embedding = EmbeddingProfile.from_json(json.loads(row["embedding_profile_json"]))
        retrieval = RetrievalProfile.from_json(json.loads(row["retrieval_profile_json"]))
        return KnowledgeBaseRecord(
            knowledge_base_id=row["knowledge_base_id"],
            connection_id=row["connection_id"],
            name=row["name"],
            status=KnowledgeBaseStatus(row["status"]),
            embedding_profile=embedding,
            retrieval_profile=retrieval,
            language_profile=row["language_profile"],
            current_generation_id=row["current_generation_id"],
            current_revision=int(row["current_revision"]),
            created_at=_parse_time(row["created_at"]),
            updated_at=_parse_time(row["updated_at"]),
        )

    @staticmethod
    def _binding_from_row(row: sqlite3.Row) -> BindingRecord:
        return BindingRecord(
            binding_id=row["binding_id"],
            workspace_identity=row["workspace_identity"],
            connection_id=row["connection_id"],
            knowledge_base_id=row["knowledge_base_id"],
            created_at=_parse_time(row["created_at"]),
        )
