"""Versioned memory, one-shot user intent, maintenance leases and durable cost accounting."""

import hashlib
import json
import re
import sqlite3
import time
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from uuid import uuid4

from jdagent.domain.json import JsonObject


def parse_memory_intent(text: str) -> tuple[str, str, str | None, str] | None:
    """Match a direct whole utterance; never recursively parse its statement payload."""
    text = text.strip()
    text = re.sub(r"^(?:请|请帮我|麻烦)(?=记住|把|忘记|重新启用|记为)", "", text, count=1)
    patterns = (
        (r"记住[：:]\s*(.+)", "remember", "workspace"),
        (r"记为我的通用偏好[：:]\s*(.+)", "remember", "user"),
        (r"把本项目记忆\s+(M[a-f0-9]+)\s+更正为[：:]\s*(.+)", "correct", "workspace"),
        (r"忘记本项目记忆\s+(M[a-f0-9]+)", "disable", "workspace"),
        (r"重新启用本项目记忆\s+(M[a-f0-9]+)", "reactivate", "workspace"),
        (r"把我的通用偏好\s+(M[a-f0-9]+)\s+更正为[：:]\s*(.+)", "correct", "user"),
        (r"忘记我的通用偏好\s+(M[a-f0-9]+)", "disable", "user"),
        (r"重新启用我的通用偏好\s+(M[a-f0-9]+)", "reactivate", "user"),
    )
    for pattern, operation, scope in patterns:
        match = re.fullmatch(pattern, text, re.DOTALL)
        if match:
            if operation == "remember":
                return operation, scope, None, match[1].strip()
            return operation, scope, match[1], match[2].strip() if operation == "correct" else ""
    return None


@dataclass(frozen=True, slots=True)
class MemoryRecord:
    memory_id: str
    version: int
    workspace: str
    scope: str
    statement: str
    state: str
    source: str
    explicit: bool
    valid_until: float | None = None
    dependencies: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class JobLease:
    job_id: str
    workspace: str
    session: str
    owner: str
    epoch: int


def _record(row: sqlite3.Row) -> MemoryRecord:
    return MemoryRecord(
        row["id"],
        row["version"],
        row["workspace"],
        row["scope"],
        row["statement"],
        row["state"],
        row["source"],
        bool(row["explicit"]),
        row["valid_until"],
        tuple(json.loads(row["dependencies"])),
    )


class MemoryStore:
    """SQLite owns memory and jobs; no mutable connection is shared across threads."""

    def __init__(self, path: Path) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with self._transaction() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS memories (
                    id TEXT, version INTEGER, workspace TEXT, scope TEXT, statement TEXT,
                    state TEXT, source TEXT, explicit INTEGER, valid_until REAL,
                    dependencies TEXT NOT NULL DEFAULT '[]', PRIMARY KEY(id,version));
                CREATE TABLE IF NOT EXISTS intents (
                    id TEXT PRIMARY KEY, workspace TEXT, session TEXT, turn TEXT, event TEXT UNIQUE,
                    operation TEXT, scope TEXT, target TEXT, expected INTEGER,
                    statement TEXT, digest TEXT, result_id TEXT, result_version INTEGER);
                CREATE TABLE IF NOT EXISTS suppressions (
                    id TEXT PRIMARY KEY, workspace TEXT, statement TEXT,
                    source TEXT, enabled INTEGER);
                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY, workspace TEXT, session TEXT, cutoff INTEGER,
                    state TEXT, owner TEXT, epoch INTEGER DEFAULT 0, expires REAL DEFAULT 0,
                    attempts INTEGER DEFAULT 0, next_run REAL DEFAULT 0, error TEXT,
                    UNIQUE(workspace,session,cutoff));
                CREATE TABLE IF NOT EXISTS sources (
                    workspace TEXT, session TEXT, digest TEXT, candidates TEXT,
                    PRIMARY KEY(workspace,session,digest));
                CREATE TABLE IF NOT EXISTS requests (
                    id TEXT PRIMARY KEY, workspace TEXT, kind TEXT, input INTEGER, output INTEGER,
                    settled INTEGER DEFAULT 0);
                CREATE TABLE IF NOT EXISTS vectors (
                    namespace TEXT, id TEXT, fingerprint TEXT, digest TEXT, vector TEXT,
                    PRIMARY KEY(namespace,id,fingerprint));
                CREATE TABLE IF NOT EXISTS request_manifests (id TEXT PRIMARY KEY, digest TEXT);
                CREATE TABLE IF NOT EXISTS extractions (
                    workspace TEXT, session TEXT, digest TEXT, result TEXT,
                    PRIMARY KEY(workspace,session,digest));
                CREATE TABLE IF NOT EXISTS intent_metadata (
                    id TEXT PRIMARY KEY, dependencies TEXT, valid_until REAL);
            """)

    @contextmanager
    def _transaction(self) -> Generator[sqlite3.Connection]:
        db = sqlite3.connect(self.path, timeout=5)
        db.row_factory = sqlite3.Row
        try:
            db.execute("PRAGMA busy_timeout=5000")
            db.execute("PRAGMA synchronous=FULL")
            db.execute("BEGIN IMMEDIATE")
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    @staticmethod
    def _latest(db: sqlite3.Connection, identity: str) -> MemoryRecord:
        row = db.execute(
            "SELECT * FROM memories WHERE id=? ORDER BY version DESC LIMIT 1", (identity,)
        ).fetchone()
        if row is None:
            raise ValueError("Unknown memory ID")
        return _record(row)

    @staticmethod
    def _insert(db: sqlite3.Connection, record: MemoryRecord) -> None:
        db.execute(
            "INSERT INTO memories VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                record.memory_id,
                record.version,
                record.workspace,
                record.scope,
                record.statement,
                record.state,
                record.source,
                int(record.explicit),
                record.valid_until,
                json.dumps(record.dependencies),
            ),
        )

    def accept_intent(
        self,
        text: str,
        *,
        workspace: str,
        session: str,
        turn: str,
        event: str,
        dependencies: tuple[str, ...] = (),
    ) -> str | None:
        parsed = parse_memory_intent(text)
        if parsed is None:
            return None
        operation, scope, target, statement = parsed
        valid_until = None
        expiry = re.search(r"\s*\[有效至[：:]([^\]]+)\]$", statement)
        if expiry:
            parsed_date = datetime.fromisoformat(expiry[1].strip())
            if parsed_date.tzinfo is None:
                raise ValueError("Memory expiry must include a timezone")
            valid_until = parsed_date.timestamp()
            statement = statement[: expiry.start()].strip()
        if not statement and operation in {"remember", "correct"}:
            raise ValueError("A memory statement must not be empty")
        with self._transaction() as db:
            expected = 0
            if target:
                current = self._latest(db, target)
                if current.scope != scope or (
                    scope == "workspace" and current.workspace != workspace
                ):
                    raise ValueError("Memory scope does not match this workspace")
                expected = current.version
                statement = statement or current.statement
                dependencies = dependencies or current.dependencies
                if valid_until is None:
                    valid_until = current.valid_until
            existing = db.execute("SELECT id FROM intents WHERE event=?", (event,)).fetchone()
            if existing:
                return str(existing[0])
            identity = uuid4().hex
            db.execute(
                "INSERT INTO intents VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    identity,
                    workspace,
                    session,
                    turn,
                    event,
                    operation,
                    scope,
                    target,
                    expected,
                    statement,
                    hashlib.sha256(statement.encode()).hexdigest(),
                    None,
                    None,
                ),
            )
            db.execute(
                "INSERT INTO intent_metadata VALUES (?,?,?)",
                (identity, json.dumps(dependencies), valid_until),
            )
            return identity

    def intent_dependencies(self, identity: str) -> tuple[str, ...]:
        with self._transaction() as db:
            row = db.execute(
                "SELECT dependencies FROM intent_metadata WHERE id=?", (identity,)
            ).fetchone()
            return tuple(json.loads(row[0])) if row else ()

    def intent_arguments(self, identity: str) -> JsonObject:
        with self._transaction() as db:
            row = db.execute("SELECT * FROM intents WHERE id=?", (identity,)).fetchone()
            if row is None:
                raise ValueError("Unknown memory intent")
            return {
                "operation": row["operation"],
                "scope": row["scope"],
                "memory_id": row["target"],
                "expected_version": row["expected"],
                "statement": row["statement"],
            }

    def apply_intent(
        self,
        identity: str,
        *,
        workspace: str,
        session: str,
        turn: str,
        write_allowed: bool,
        dependencies_verified: bool = False,
    ) -> MemoryRecord:
        if not write_allowed:
            raise PermissionError("Memory write denied by current capability or write ceiling")
        with self._transaction() as db:
            row = db.execute("SELECT * FROM intents WHERE id=?", (identity,)).fetchone()
            if row is None or (row["workspace"], row["session"], row["turn"]) != (
                workspace,
                session,
                turn,
            ):
                raise PermissionError("Missing or expired direct memory intent")
            if row["result_id"]:
                result = db.execute(
                    "SELECT * FROM memories WHERE id=? AND version=?",
                    (row["result_id"], row["result_version"]),
                ).fetchone()
                return _record(result)
            current = self._latest(db, row["target"]) if row["target"] else None
            if current is not None and current.version != row["expected"]:
                raise ValueError("Memory expected version changed; clarify before retrying")
            operation = row["operation"]
            metadata = db.execute(
                "SELECT * FROM intent_metadata WHERE id=?", (identity,)
            ).fetchone()
            dependencies = tuple(json.loads(metadata["dependencies"])) if metadata else ()
            valid_until = metadata["valid_until"] if metadata else None
            state = "active"
            if operation == "disable" or (
                current and current.state == "disabled" and operation != "reactivate"
            ):
                state = "disabled"
            elif dependencies and not dependencies_verified:
                state = "needs_verification"
            elif valid_until is not None and valid_until <= time.time():
                state = "expired"
            item = MemoryRecord(
                row["target"] or f"M{uuid4().hex[:16]}",
                row["expected"] + 1,
                workspace if row["scope"] == "workspace" else "",
                row["scope"],
                row["statement"],
                state,
                f"{workspace}:{session}:{row['event']}",
                True,
                valid_until=valid_until,
                dependencies=dependencies,
            )
            self._insert(db, item)
            if operation == "disable":
                db.execute(
                    "INSERT OR REPLACE INTO suppressions VALUES (?,?,?,?,1)",
                    (
                        item.memory_id,
                        workspace if item.scope == "workspace" else "",
                        item.statement,
                        item.source,
                    ),
                )
            elif operation == "reactivate" and state == "active":
                db.execute("UPDATE suppressions SET enabled=0 WHERE id=?", (item.memory_id,))
            db.execute(
                "UPDATE intents SET result_id=?,result_version=? WHERE id=?",
                (item.memory_id, item.version, identity),
            )
            return item

    def versions(self, identity: str) -> tuple[MemoryRecord, ...]:
        with self._transaction() as db:
            records = tuple(
                _record(row)
                for row in db.execute(
                    "SELECT * FROM memories WHERE id=? ORDER BY version", (identity,)
                )
            )
            return tuple(replace(item, state="superseded") for item in records[:-1]) + records[-1:]

    def recall(
        self, workspace: str, *, include_inactive: bool = False, now: float | None = None
    ) -> tuple[MemoryRecord, ...]:
        now = time.time() if now is None else now
        with self._transaction() as db:
            rows = db.execute(
                """SELECT m.* FROM memories m WHERE
                (m.workspace=? OR m.scope='user') AND version=(SELECT MAX(version) FROM
memories x WHERE x.id=m.id)""",
                (workspace,),
            ).fetchall()
            result: list[MemoryRecord] = []
            for row in rows:
                item = _record(row)
                state = (
                    "expired"
                    if item.valid_until is not None and item.valid_until <= now
                    else item.state
                )
                if state != item.state:
                    item = MemoryRecord(
                        item.memory_id,
                        item.version + 1,
                        item.workspace,
                        item.scope,
                        item.statement,
                        state,
                        item.source,
                        item.explicit,
                        item.valid_until,
                        item.dependencies,
                    )
                    self._insert(db, item)
                if include_inactive or item.state == "active":
                    result.append(item)
            return tuple(result)

    def publish_candidate(
        self,
        workspace: str,
        statement: str,
        *,
        source: str,
        dependencies: tuple[str, ...] = (),
        verified: bool = True,
        db: sqlite3.Connection | None = None,
    ) -> MemoryRecord:
        if db is None:
            with self._transaction() as connection:
                return self.publish_candidate(
                    workspace,
                    statement,
                    source=source,
                    dependencies=dependencies,
                    verified=verified,
                    db=connection,
                )
        same = db.execute(
            "SELECT * FROM memories WHERE workspace=? AND statement=? AND source=? "
            "ORDER BY version DESC LIMIT 1",
            (workspace, statement, source),
        ).fetchone()
        if same:
            return self._latest(db, same["id"])
        # If equivalence cannot be excluded, never revive a suppressed fact by paraphrasing.
        suppressed = db.execute(
            "SELECT 1 FROM suppressions WHERE workspace IN (?, '') AND enabled=1 LIMIT 1",
            (workspace,),
        ).fetchone()
        explicit = db.execute(
            "SELECT 1 FROM memories WHERE workspace IN (?, '') AND explicit=1 LIMIT 1", (workspace,)
        ).fetchone()
        state = "needs_verification" if suppressed or explicit or not verified else "active"
        item = MemoryRecord(
            f"M{uuid4().hex[:16]}",
            1,
            workspace,
            "workspace",
            statement,
            state,
            source,
            False,
            dependencies=dependencies,
        )
        self._insert(db, item)
        return item

    def invalidate_dependency(self, dependency: str) -> None:
        with self._transaction() as db:
            rows = db.execute(
                "SELECT * FROM memories m WHERE version=(SELECT MAX(version) FROM "
                "memories x WHERE x.id=m.id)"
            ).fetchall()
            for row in rows:
                item = _record(row)
                if dependency in item.dependencies and item.state == "active":
                    self._insert(
                        db,
                        MemoryRecord(
                            item.memory_id,
                            item.version + 1,
                            item.workspace,
                            item.scope,
                            item.statement,
                            "needs_verification",
                            item.source,
                            item.explicit,
                            item.valid_until,
                            item.dependencies,
                        ),
                    )

    def schedule(self, workspace: str, session: str, cutoff: int) -> str:
        identity = hashlib.sha256(f"{workspace}:{session}:{cutoff}".encode()).hexdigest()
        with self._transaction() as db:
            db.execute(
                "INSERT OR IGNORE INTO jobs(id,workspace,session,cutoff,state) VALUES "
                "(?,?,?,?,'pending')",
                (identity, workspace, session, cutoff),
            )
        return identity

    def claim(
        self, workspace: str, owner: str, *, now: float | None = None, ttl: float = 120
    ) -> JobLease | None:
        now = time.time() if now is None else now
        with self._transaction() as db:
            if db.execute(
                "SELECT 1 FROM jobs WHERE workspace=? AND state='running' AND expires>?",
                (workspace, now),
            ).fetchone():
                return None
            row = db.execute(
                "SELECT * FROM jobs WHERE workspace=? AND attempts<3 AND next_run<=? "
                "AND (state='pending' OR (state='running' AND expires<=?)) ORDER BY "
                "rowid LIMIT 1",
                (workspace, now, now),
            ).fetchone()
            if row is None:
                return None
            epoch = row["epoch"] + 1
            db.execute(
                "UPDATE jobs SET "
                "state='running',owner=?,epoch=?,expires=?,attempts=attempts+1 WHERE "
                "id=?",
                (owner, epoch, now + ttl, row["id"]),
            )
            return JobLease(row["id"], workspace, row["session"], owner, epoch)

    @staticmethod
    def _check_lease(db: sqlite3.Connection, lease: JobLease, now: float) -> None:
        row = db.execute(
            "SELECT 1 FROM jobs WHERE id=? AND state='running' AND owner=? AND "
            "epoch=? AND expires>?",
            (lease.job_id, lease.owner, lease.epoch, now),
        ).fetchone()
        if row is None:
            raise ValueError("Maintenance lease expired or superseded")

    def heartbeat(self, lease: JobLease, *, now: float | None = None) -> None:
        now = time.time() if now is None else now
        with self._transaction() as db:
            self._check_lease(db, lease, now)
            db.execute("UPDATE jobs SET expires=? WHERE id=?", (now + 120, lease.job_id))

    def finish(
        self,
        lease: JobLease,
        *,
        now: float | None = None,
        error: str | None = None,
        cancelled: bool = False,
        sources: tuple[tuple[str, str, str], ...] = (),
        candidates: tuple[tuple[str, str], ...] = (),
        candidate_dependencies: tuple[tuple[str, tuple[str, ...]], ...] = (),
        deferred: bool = False,
    ) -> None:
        now = time.time() if now is None else now
        with self._transaction() as db:
            self._check_lease(db, lease, now)
            if error is None:
                for statement, source in candidates:
                    self.publish_candidate(
                        lease.workspace,
                        statement,
                        source=source,
                        dependencies=dict(candidate_dependencies).get(source, ()),
                        db=db,
                    )
                for session, digest, data in sources:
                    db.execute(
                        "INSERT OR IGNORE INTO sources VALUES (?,?,?,?)",
                        (lease.workspace, session, digest, data),
                    )
            attempts = db.execute(
                "SELECT attempts FROM jobs WHERE id=?", (lease.job_id,)
            ).fetchone()[0]
            state = (
                "succeeded"
                if error is None
                else ("failed" if attempts >= 3 and not cancelled else "pending")
            )
            db.execute(
                "UPDATE jobs SET state=?,error=?,next_run=?,expires=0 WHERE id=?",
                (
                    state,
                    error,
                    now + 60 if error and not cancelled else 0,
                    lease.job_id,
                ),
            )
            if deferred:
                if sources:
                    # A bounded source batch gets a fresh job budget; this is not a retry reset.
                    continuation = hashlib.sha256(
                        (lease.job_id + json.dumps(sources)).encode()
                    ).hexdigest()
                    db.execute(
                        "INSERT OR IGNORE INTO jobs(id,workspace,session,cutoff,state) "
                        "VALUES (?,?,?,?,'pending')",
                        (continuation, lease.workspace, lease.session, -int(continuation[:12], 16)),
                    )
                else:
                    db.execute(
                        "UPDATE jobs SET state='pending',attempts=0,next_run=? WHERE id=?",
                        (now + 60, lease.job_id),
                    )
            elif cancelled:
                db.execute("UPDATE jobs SET attempts=MAX(0,attempts-1) WHERE id=?", (lease.job_id,))

    def source_done(self, workspace: str, session: str, digest: str) -> bool:
        with self._transaction() as db:
            return (
                db.execute(
                    "SELECT 1 FROM sources WHERE workspace=? AND session=? AND digest=?",
                    (workspace, session, digest),
                ).fetchone()
                is not None
            )

    def load_extraction(self, workspace: str, session: str, digest: str) -> str | None:
        with self._transaction() as db:
            row = db.execute(
                "SELECT result FROM extractions WHERE workspace=? AND session=? AND digest=?",
                (workspace, session, digest),
            ).fetchone()
            return str(row[0]) if row is not None else None

    def save_extraction(self, lease: JobLease, session: str, digest: str, result: str) -> None:
        with self._transaction() as db:
            self._check_lease(db, lease, time.time())
            db.execute(
                "INSERT OR IGNORE INTO extractions VALUES (?,?,?,?)",
                (lease.workspace, session, digest, result),
            )

    def job_status(self, identity: str) -> str:
        with self._transaction() as db:
            row = db.execute("SELECT state FROM jobs WHERE id=?", (identity,)).fetchone()
            return str(row[0]) if row else "missing"

    def reserve(
        self,
        request: str,
        workspace: str,
        kind: str,
        input_tokens: int,
        output_tokens: int,
        *,
        input_limit: int,
        output_limit: int,
        maintenance_input_limit: int | None = None,
        maintenance_output_limit: int | None = None,
        manifest: str | None = None,
    ) -> None:
        with self._transaction() as db:
            if kind not in {"task", "compaction", "citation_repair"}:
                count = db.execute(
                    "SELECT COUNT(*) FROM requests WHERE kind=?", (kind,)
                ).fetchone()[0]
                if count >= 8:
                    raise ValueError("Persistent maintenance model-call budget exhausted")
            if db.execute("SELECT 1 FROM requests WHERE id=?", (request,)).fetchone():
                raise ValueError("Request ID already reserved")
            used = db.execute(
                "SELECT COALESCE(SUM(input),0),COALESCE(SUM(output),0) FROM requests "
                "WHERE workspace=?",
                (workspace,),
            ).fetchone()
            if used[0] + input_tokens > input_limit or used[1] + output_tokens > output_limit:
                raise ValueError("Persistent total token budget exhausted")
            if kind not in {"task", "citation_repair"}:
                maintenance_used = db.execute(
                    "SELECT COALESCE(SUM(input),0),COALESCE(SUM(output),0) FROM requests "
                    "WHERE workspace=? AND kind NOT IN ('task','citation_repair')",
                    (workspace,),
                ).fetchone()
                if (
                    maintenance_input_limit is not None
                    and maintenance_used[0] + input_tokens > maintenance_input_limit
                ) or (
                    maintenance_output_limit is not None
                    and maintenance_used[1] + output_tokens > maintenance_output_limit
                ):
                    raise ValueError("Persistent maintenance token budget exhausted")
            db.execute(
                "INSERT INTO requests VALUES (?,?,?,?,?,0)",
                (request, workspace, kind, input_tokens, output_tokens),
            )
            if manifest is not None:
                db.execute("INSERT INTO request_manifests VALUES (?,?)", (request, manifest))

    def settle(self, request: str, input_tokens: int, output_tokens: int) -> None:
        with self._transaction() as db:
            db.execute(
                "UPDATE requests SET input=?,output=?,settled=1 WHERE id=? AND settled=0",
                (input_tokens, output_tokens, request),
            )

    def bind_manifest(self, request: str, digest: str) -> None:
        with self._transaction() as db:
            db.execute("INSERT INTO request_manifests VALUES (?,?)", (request, digest))

    def costs(self, workspace: str) -> tuple[int, int]:
        with self._transaction() as db:
            row = db.execute(
                "SELECT COALESCE(SUM(input),0),COALESCE(SUM(output),0) FROM requests "
                "WHERE workspace=?",
                (workspace,),
            ).fetchone()
            return int(row[0]), int(row[1])

    def read_vector(
        self, namespace: str, identity: str, fingerprint: str, digest: str
    ) -> tuple[float, ...] | None:
        with self._transaction() as db:
            row = db.execute(
                "SELECT vector FROM vectors WHERE namespace=? AND id=? AND "
                "fingerprint=? AND digest=?",
                (namespace, identity, fingerprint, digest),
            ).fetchone()
            return tuple(float(x) for x in json.loads(row[0])) if row else None

    def write_vector(
        self,
        namespace: str,
        identity: str,
        fingerprint: str,
        digest: str,
        vector: tuple[float, ...],
    ) -> None:
        with self._transaction() as db:
            db.execute(
                "INSERT OR REPLACE INTO vectors VALUES (?,?,?,?,?)",
                (namespace, identity, fingerprint, digest, json.dumps(vector)),
            )
