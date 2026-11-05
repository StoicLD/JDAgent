"""Durable, adapter-independent admission for bounded verification experiments.

This is test infrastructure, separate from the product accounting ledger. It stores
only request IDs, experiment labels, and counts, never request or response content.
"""

import asyncio
import sqlite3
from collections.abc import AsyncGenerator, Generator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from jdagent.context_budget import InputEstimator
from jdagent.domain.model import (
    ModelCapabilities,
    ModelErrorCategory,
    ModelEvent,
    ModelFailed,
    ModelRequest,
    Usage,
    UsageReported,
)
from jdagent.model_stream import closing_model_stream
from jdagent.ports import ModelPort

COMBINATIONS = frozenset(
    {"native/native", "native/langchain", "langgraph/native", "langgraph/langchain"}
)
CALL_KINDS = frozenset({"task", "compaction", "citation_repair", "memory"})


class ExperimentBudgetError(ValueError):
    """An experiment cannot admit a request or cannot verify its persisted accounting."""


def _integer(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ExperimentBudgetError("Invalid experiment token or call count")
    return value


def _label(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ExperimentBudgetError("Experiment identity is required")
    return value


@dataclass(frozen=True, slots=True)
class ExperimentLimits:
    calls: int
    input_tokens: int
    output_tokens: int

    def __post_init__(self) -> None:
        for value in (self.calls, self.input_tokens, self.output_tokens):
            if _integer(value) == 0:
                raise ExperimentBudgetError("Experiment limits must be positive")


@dataclass(frozen=True, slots=True)
class ExperimentEntry:
    request_id: str
    combination: str
    attempt: int
    call_kind: str
    reserved_input: int
    reserved_output: int
    input_tokens: int
    output_tokens: int
    status: str


class ExperimentLedger:
    """Serialize admission and preserve reservations across processes and restarts.

    Limits and experiment identity are fixed when creating the ledger. A missing,
    corrupt, or differently configured existing ledger never silently starts over.
    """

    def __init__(self, path: Path, experiment_id: str, limits: ExperimentLimits) -> None:
        self.path = path.resolve()
        self.experiment_id = _label(experiment_id)
        self.limits = limits
        self.path.parent.mkdir(parents=True, exist_ok=True)
        existed = self.path.exists()
        connection = sqlite3.connect(self.path, isolation_level=None, timeout=10)
        try:
            connection.execute("BEGIN IMMEDIATE")
            present = connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='experiment'"
            ).fetchone()
            if present is None:
                if existed:
                    raise ExperimentBudgetError("Existing experiment ledger is incomplete")
                connection.execute(
                    "CREATE TABLE experiment (id INTEGER PRIMARY KEY CHECK(id=1), "
                    "schema_version INTEGER NOT NULL, experiment_id TEXT NOT NULL, "
                    "calls INTEGER NOT NULL, input_tokens INTEGER NOT NULL, "
                    "output_tokens INTEGER NOT NULL)"
                )
                connection.execute(
                    "CREATE TABLE requests (request_id TEXT PRIMARY KEY, "
                    "combination TEXT NOT NULL, attempt INTEGER NOT NULL, "
                    "call_kind TEXT NOT NULL, reserved_input INTEGER NOT NULL, "
                    "reserved_output INTEGER NOT NULL, input_tokens INTEGER NOT NULL, "
                    "output_tokens INTEGER NOT NULL, status TEXT NOT NULL)"
                )
                connection.execute(
                    "INSERT INTO experiment VALUES (1, 1, ?, ?, ?, ?)", self._configuration()
                )
            self._validate(connection)
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _configuration(self) -> tuple[str, int, int, int]:
        return (
            self.experiment_id,
            self.limits.calls,
            self.limits.input_tokens,
            self.limits.output_tokens,
        )

    def _validate(self, connection: sqlite3.Connection) -> None:
        row = connection.execute(
            "SELECT schema_version, experiment_id, calls, input_tokens, output_tokens "
            "FROM experiment WHERE id=1"
        ).fetchone()
        if row is None or tuple(row) != (1, *self._configuration()):
            raise ExperimentBudgetError(
                "Experiment identity or limits differ from the fixed ledger"
            )
        # Reading the rows also rejects malformed counts before admitting another call.
        self._entries(connection)

    @contextmanager
    def _transaction(self) -> Generator[sqlite3.Connection]:
        connection = sqlite3.connect(
            self.path.as_uri() + "?mode=rw", uri=True, isolation_level=None, timeout=10
        )
        try:
            connection.execute("BEGIN IMMEDIATE")
            self._validate(connection)
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    @staticmethod
    def _entries(connection: sqlite3.Connection) -> tuple[ExperimentEntry, ...]:
        entries: list[ExperimentEntry] = []
        for row in connection.execute("SELECT * FROM requests ORDER BY rowid"):
            entry = ExperimentEntry(
                _label(row[0]),
                _label(row[1]),
                _integer(row[2]),
                _label(row[3]),
                _integer(row[4]),
                _integer(row[5]),
                _integer(row[6]),
                _integer(row[7]),
                _label(row[8]),
            )
            if (
                entry.combination not in COMBINATIONS
                or entry.attempt not in {1, 2, 3}
                or entry.call_kind not in CALL_KINDS
                or entry.status not in {"reserved", "settled", "unknown"}
                or (
                    entry.status != "settled"
                    and (
                        entry.input_tokens < entry.reserved_input
                        or entry.output_tokens < entry.reserved_output
                    )
                )
            ):
                raise ExperimentBudgetError("Experiment ledger contains an invalid reservation")
            entries.append(entry)
        return tuple(entries)

    def entries(self) -> tuple[ExperimentEntry, ...]:
        with self._transaction() as connection:
            return self._entries(connection)

    def _over_budget(self, connection: sqlite3.Connection) -> bool:
        entries = self._entries(connection)
        return (
            len(entries) > self.limits.calls
            or sum(e.input_tokens for e in entries) > self.limits.input_tokens
            or sum(e.output_tokens for e in entries) > self.limits.output_tokens
        )

    def reserve(self, request: ModelRequest, combination: str, attempt: int) -> str:
        request_id = _label(request.metadata.get("request_id"))
        kind = request.metadata.get("call_kind", "task")
        if (
            combination not in COMBINATIONS
            or _integer(attempt) not in {1, 2, 3}
            or kind not in CALL_KINDS
        ):
            raise ExperimentBudgetError("Unknown experiment combination, attempt, or call kind")
        output = _integer(request.settings.max_output_tokens)
        if output == 0:
            raise ExperimentBudgetError("A bounded output reservation is required")
        estimate = InputEstimator().measure(request).input_tokens
        with self._transaction() as connection:
            entries = self._entries(connection)
            if any(e.request_id == request_id for e in entries):
                raise ExperimentBudgetError("Experiment request ID was already admitted")
            if (
                len(entries) >= self.limits.calls
                or sum(e.input_tokens for e in entries) + estimate > self.limits.input_tokens
                or sum(e.output_tokens for e in entries) + output > self.limits.output_tokens
            ):
                raise ExperimentBudgetError("Experiment call or token budget exhausted")
            connection.execute(
                "INSERT INTO requests VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'reserved')",
                (request_id, combination, attempt, kind, estimate, output, estimate, output),
            )
        return request_id

    def finish(self, request_id: str, usage: Usage | None, *, drained: bool) -> bool:
        if usage is not None:
            _integer(usage.input_tokens)
            _integer(usage.output_tokens)
        with self._transaction() as connection:
            entry = next((e for e in self._entries(connection) if e.request_id == request_id), None)
            if entry is None:
                raise ExperimentBudgetError("Cannot settle an unreserved experiment request")
            known = drained and usage is not None
            actual_input = usage.input_tokens if usage is not None else 0
            actual_output = usage.output_tokens if usage is not None else 0
            charged_input = actual_input if known else max(entry.reserved_input, actual_input)
            charged_output = actual_output if known else max(entry.reserved_output, actual_output)
            status = "settled" if known else "unknown"
            if entry.status != "reserved":
                if (entry.input_tokens, entry.output_tokens, entry.status) != (
                    charged_input,
                    charged_output,
                    status,
                ):
                    raise ExperimentBudgetError("Experiment request was already finalized")
            else:
                connection.execute(
                    "UPDATE requests SET input_tokens=?, output_tokens=?, status=? "
                    "WHERE request_id=?",
                    (charged_input, charged_output, status, request_id),
                )
            return self._over_budget(connection)


class BudgetedModel:
    """Borrow a model and admit every external attempt through the shared ledger."""

    def __init__(
        self, model: ModelPort, ledger: ExperimentLedger, combination: str, attempt: int
    ) -> None:
        self.model, self.ledger = model, ledger
        self.combination, self.attempt = combination, attempt

    @property
    def capabilities(self) -> ModelCapabilities:
        return self.model.capabilities

    async def stream(self, request: ModelRequest) -> AsyncGenerator[ModelEvent]:
        try:
            request_id = await asyncio.to_thread(
                self.ledger.reserve, request, self.combination, self.attempt
            )
        except ExperimentBudgetError as error:
            yield ModelFailed(
                ModelErrorCategory.PERMISSION, str(error), provider_code="experiment_budget"
            )
            return
        usage: Usage | None = None
        drained = False
        over_budget = False
        try:
            async with closing_model_stream(self.model.stream(request)) as events:
                async for event in events:
                    if isinstance(event, UsageReported):
                        previous = usage or Usage()
                        usage = Usage(
                            max(previous.input_tokens, _integer(event.usage.input_tokens)),
                            max(previous.output_tokens, _integer(event.usage.output_tokens)),
                        )
                    yield event
                drained = True
        finally:
            over_budget = await asyncio.to_thread(
                self.ledger.finish, request_id, usage, drained=drained
            )
        if over_budget:
            yield ModelFailed(
                ModelErrorCategory.PERMISSION,
                "Provider usage exceeded the experiment budget",
                provider_code="experiment_budget",
            )
