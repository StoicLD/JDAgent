"""Coordinate one turn without embedding adapter details in the agent loop."""

import asyncio
import json
import re
from collections.abc import Callable
from dataclasses import asdict, dataclass
from hashlib import sha256
from pathlib import Path
from typing import Protocol
from uuid import uuid4

from jdagent.application.session_recovery import LogicalRecovery, classify_logical_recovery
from jdagent.context_archive import encode_json
from jdagent.context_runtime import ContextService, WindowSnapshot
from jdagent.core.loop import AgentLoop, CancellationToken, TurnResult
from jdagent.domain.errors import SessionError, SessionErrorCode, StopReason
from jdagent.domain.events import (
    AssistantMessageCompletedPayload,
    ConstraintRevokedPayload,
    RecoverySnapshotPayload,
    RetrievalBaseRecord,
    RetrievalBudgetRecord,
    RetrievalEvidenceRecord,
    RuntimeEventType,
    SessionStartedPayload,
    SideEffectResolutionPayload,
    TurnFailedPayload,
    TurnRetrievalRecordedPayload,
    UserMessagePayload,
)
from jdagent.domain.tools import ToolExecutionContext
from jdagent.eventing import EventJournal
from jdagent.knowledge.catalog import KnowledgeCatalog
from jdagent.knowledge.embedding import AdaptiveEmbedding
from jdagent.knowledge.errors import KnowledgeError
from jdagent.knowledge.index import KnowledgeIndex
from jdagent.knowledge.preparation import CatalogBaseResolver, TurnKnowledgePreparation
from jdagent.knowledge.ptk import (
    PreparedTurnKnowledge,
    RetrievalOutcome,
    empty_prepared_knowledge,
)
from jdagent.memory_service import MemoryService
from jdagent.observability import TraceProjection
from jdagent.ports import EventObserver, RuntimeJournal, SessionPort


class AgentLoopFactory(Protocol):
    """Create a configured loop for a per-session journal."""

    def create(self, journal: RuntimeJournal) -> AgentLoop: ...

    def context_for(self, session_id: str) -> ContextService | None: ...

    def prepare_turn(self) -> None: ...


@dataclass(frozen=True, slots=True)
class CoordinatedTurn:
    """Application result with stable IDs and its trace projection."""

    session_id: str
    turn_id: str
    result: TurnResult
    trace: TraceProjection
    knowledge: PreparedTurnKnowledge
    maintenance_job: str | None = None


def retrieval_payload(ptk: PreparedTurnKnowledge) -> TurnRetrievalRecordedPayload:
    return TurnRetrievalRecordedPayload(
        ptk.outcome.value,
        ptk.turn_token,
        tuple(
            RetrievalBaseRecord(
                item.binding_id,
                item.connection_id,
                item.knowledge_base_id,
                item.kb_name,
                item.status.value,
                None if item.failure_reason is None else item.failure_reason.value,
                item.generation_id,
                item.revision,
                item.physical_collection,
                item.embedding_profile_fingerprint,
                item.retrieval_profile_fingerprint,
                item.hit_count,
                item.selected_evidence_count,
            )
            for item in ptk.bases
        ),
        tuple(
            RetrievalEvidenceRecord(
                item.evidence_id,
                item.ordinal,
                item.knowledge_base_id,
                item.source_id,
                item.source_version_id,
                item.snapshot_id,
                item.locator,
                item.locator_schema_version,
                item.content_hash,
            )
            for item in ptk.evidence
        ),
        RetrievalBudgetRecord(
            ptk.budget.dense_top_k,
            ptk.budget.bm25_top_k,
            ptk.budget.fused_child_count,
            ptk.budget.rerank_pool_size,
            ptk.budget.parent_count,
            ptk.budget.evidence_tokens,
            ptk.budget.evidence_token_limit,
            ptk.budget.parent_limit,
        ),
        ptk.degradations,
    )


class TurnCoordinator:
    """Own the session-input-to-turn-result application lifecycle."""

    def __init__(
        self,
        *,
        session: SessionPort,
        workspace: Path,
        loop_factory: AgentLoopFactory,
        event_observers: tuple[EventObserver, ...] = (),
        workspace_identity: str | None = None,
        knowledge_catalog: Path | None = None,
        knowledge_backups: Path | None = None,
        knowledge_index: Path | None = None,
        memory: MemoryService | None = None,
    ) -> None:
        self._session = session
        self._workspace = workspace
        self._loop_factory = loop_factory
        self._event_observers = event_observers
        self._workspace_identity = workspace_identity
        self._knowledge_catalog = knowledge_catalog
        self._knowledge_backups = knowledge_backups
        self._knowledge_index = knowledge_index
        self.memory = memory
        self.after_output: Callable[[], None] = lambda: None
        self._owned_sessions: set[str] = set()
        self._running: set[str] = set()
        self._compacting: set[str] = set()
        self._pending_compactions: set[str] = set()

    async def aclose(self) -> None:
        for session_id in tuple(self._owned_sessions):
            await self._session.release(session_id)
            self._owned_sessions.discard(session_id)
        if self.memory is not None:
            self.memory.active_sessions.clear()
            self.memory.views.clear()

    async def context_window(self, session_id: str) -> WindowSnapshot:
        service = self._loop_factory.context_for(session_id)
        if service is None:
            journal = await EventJournal.open(self._session, session_id, require_existing=True)
            self._loop_factory.create(journal)
            service = self._loop_factory.context_for(session_id)
        assert service is not None
        return await service.preview_window()

    async def compact(self, session_id: str) -> str:
        if session_id in self._running or session_id in self._compacting:
            self._pending_compactions.add(session_id)
            service = self._loop_factory.context_for(session_id)
            if service is not None:
                service.request_compaction()
            return "compaction queued; waiting for the complete tool batch"
        self._compacting.add(session_id)
        try:
            await self._session.acquire(session_id)
            self._owned_sessions.add(session_id)
            journal = await EventJournal.open(self._session, session_id, require_existing=True)
            self._loop_factory.create(journal)
            service = self._loop_factory.context_for(session_id)
            assert service is not None
            snapshot = await service.compact_now()
            return (
                f"compaction={snapshot.compaction_status}; input={snapshot.estimate.input_tokens}"
            )
        finally:
            self._compacting.discard(session_id)
            self._pending_compactions.discard(session_id)

    async def release_session(self, session_id: str) -> None:
        if session_id in self._running or session_id in self._compacting:
            raise ValueError("Cannot release a running session")
        await self._session.release(session_id)
        self._owned_sessions.discard(session_id)
        if self.memory is not None:
            self.memory.active_sessions.discard(session_id)
            self.memory.views.pop(session_id, None)

    async def send(
        self,
        user_text: str,
        *,
        session_id: str | None = None,
        new_session_id: str | None = None,
        cancellation: CancellationToken | None = None,
    ) -> CoordinatedTurn:
        if session_id is not None and new_session_id is not None:
            raise ValueError("session_id and new_session_id are mutually exclusive")
        identity = session_id or new_session_id or str(uuid4())
        if identity in self._running or identity in self._compacting:
            raise SessionError(SessionErrorCode.BUSY, "session_busy: operation already running")
        self._running.add(identity)
        try:
            return await self._send_owned(
                user_text,
                session_id=session_id,
                new_session_id=identity if session_id is None else None,
                cancellation=cancellation,
            )
        finally:
            self._running.discard(identity)
            self._pending_compactions.discard(identity)

    async def _send_owned(
        self,
        user_text: str,
        *,
        session_id: str | None = None,
        new_session_id: str | None = None,
        cancellation: CancellationToken | None = None,
    ) -> CoordinatedTurn:
        """Append a user message and drive exactly one agent turn."""

        if not user_text.strip():
            raise ValueError("user_text must not be empty")
        await asyncio.to_thread(self._loop_factory.prepare_turn)
        if session_id is not None and new_session_id is not None:
            raise ValueError("session_id and new_session_id are mutually exclusive")
        actual_session_id = session_id or new_session_id or str(uuid4())
        await self._session.acquire(actual_session_id)
        self._owned_sessions.add(actual_session_id)
        if self.memory is not None:
            self.memory.active_sessions.add(actual_session_id)
        turn_id = str(uuid4())
        turn_token = turn_id.replace("-", "")[:8]
        trace = TraceProjection()
        journal = await EventJournal.open(
            self._session,
            actual_session_id,
            observers=(trace, *self._event_observers),
            require_existing=session_id is not None,
        )
        if self.memory is not None:
            self.memory.views[actual_session_id] = lambda: journal.events
        if (
            classify_logical_recovery(tuple(journal.events))
            is LogicalRecovery.UNCERTAIN_SIDE_EFFECT
        ):
            raise SessionError(
                SessionErrorCode.CORRUPT_EVENT,
                "Session has uncertain side effects; inspect logs and use /resume",
            )
        if not journal.events:
            await journal.record(
                None,
                RuntimeEventType.SESSION_STARTED,
                SessionStartedPayload(
                    name=f"session-{actual_session_id[:8]}",
                    workspace_identity=self._workspace_identity,
                ),
            )
        user_event = await journal.record(
            turn_id,
            RuntimeEventType.USER_MESSAGE,
            UserMessagePayload(user_text),
        )
        memory_intent = None
        resolution = re.fullmatch(
            r"确认父会话 ([A-Za-z0-9._-]+) 操作 ([A-Za-z0-9._-]+) (已完成|未执行)，依据：(.+)",
            user_text.strip(),
        )
        if resolution:
            parent, call_id, conclusion, evidence = resolution.groups()
            refs = [
                event.payload
                for event in journal.events
                if isinstance(event.payload, RecoverySnapshotPayload)
                and event.payload.parent_session_id == parent
            ]
            known = any(
                json.loads(operation).get("call_id") == call_id
                for ref in refs
                for operation in ref.unknown_operations
            )
            if not known:
                await journal.record(
                    turn_id,
                    RuntimeEventType.TURN_FAILED,
                    TurnFailedPayload(
                        StopReason.INTERNAL_ERROR,
                        "invalid_resolution",
                        "Unknown recovery operation",
                        0,
                        0,
                    ),
                )
                raise ValueError(
                    "Resolution must name an unknown operation of this recovery Session"
                )
            await journal.record(
                turn_id,
                RuntimeEventType.SIDE_EFFECT_RESOLVED,
                SideEffectResolutionPayload(
                    parent, call_id, user_event.event_id, conclusion, evidence
                ),
            )
        revoke = re.fullmatch(r"撤销本会话约束 C([1-9][0-9]*)", user_text.strip())
        if revoke:
            sequence = int(revoke[1])
            if not any(
                event.sequence == sequence
                and isinstance(event.payload, UserMessagePayload)
                and event.sequence < user_event.sequence
                for event in journal.events
            ):
                await journal.record(
                    turn_id,
                    RuntimeEventType.TURN_FAILED,
                    TurnFailedPayload(
                        StopReason.INTERNAL_ERROR,
                        "invalid_constraint",
                        "Unknown constraint ID",
                        0,
                        0,
                    ),
                )
                raise ValueError("Unknown constraint ID")
            await journal.record(
                turn_id,
                RuntimeEventType.CONSTRAINT_REVOKED,
                ConstraintRevokedPayload(sequence, user_event.event_id),
            )
        if self.memory is not None:
            citations = next(
                (
                    event.payload.citations
                    for event in reversed(journal.events)
                    if isinstance(event.payload, AssistantMessageCompletedPayload)
                ),
                (),
            )
            try:
                memory_intent = await asyncio.to_thread(
                    self.memory.store.accept_intent,
                    user_text,
                    workspace=self.memory.workspace,
                    session=actual_session_id,
                    turn=turn_id,
                    event=user_event.event_id,
                    dependencies=tuple(encode_json(asdict(item)).decode() for item in citations),
                )
            except ValueError:
                await journal.record(
                    turn_id,
                    RuntimeEventType.TURN_FAILED,
                    TurnFailedPayload(
                        StopReason.INTERNAL_ERROR,
                        "invalid_memory_intent",
                        "Memory target, version or expiry is invalid",
                        0,
                        0,
                    ),
                )
                raise
            if memory_intent is not None:
                self.memory.intents[turn_id] = memory_intent

        cancellation = cancellation or CancellationToken()
        if cancellation.cancelled:
            result = await self._fail_before_loop(
                journal,
                turn_id,
                "knowledge_preparation_cancelled",
            )
            empty = empty_prepared_knowledge(turn_id, turn_token, "")
            return CoordinatedTurn(actual_session_id, turn_id, result, trace, empty)

        knowledge = await self._prepare(turn_id, turn_token, user_text)
        await journal.record(
            turn_id,
            RuntimeEventType.TURN_RETRIEVAL_RECORDED,
            retrieval_payload(knowledge),
        )
        if cancellation.cancelled:
            result = await self._fail_before_loop(
                journal,
                turn_id,
                "process_interrupted",
            )
            return CoordinatedTurn(actual_session_id, turn_id, result, trace, knowledge)

        loop = self._loop_factory.create(journal)
        if actual_session_id in self._pending_compactions:
            service = self._loop_factory.context_for(actual_session_id)
            assert service is not None
            service.request_compaction()
            self._pending_compactions.discard(actual_session_id)
        result = await loop.run(
            turn_id,
            ToolExecutionContext(
                actual_session_id,
                turn_id,
                self._workspace,
                tuple(
                    event.payload.parent_session_id
                    for event in journal.events
                    if isinstance(event.payload, RecoverySnapshotPayload)
                ),
                memory_intent=memory_intent,
            ),
            cancellation=cancellation,
            knowledge=knowledge,
        )
        maintenance_job = None
        if self.memory is not None:
            first = next(
                (
                    event
                    for event in journal.events
                    if event.event_type is RuntimeEventType.TURN_COMPLETED
                ),
                None,
            )
            if first is not None:
                maintenance_job = await asyncio.to_thread(
                    self.memory.store.schedule,
                    self.memory.workspace,
                    actual_session_id,
                    first.sequence,
                )
        return CoordinatedTurn(
            actual_session_id, turn_id, result, trace, knowledge, maintenance_job
        )

    async def _fail_before_loop(
        self, journal: EventJournal, turn_id: str, error_category: str
    ) -> TurnResult:
        await journal.record(
            turn_id,
            RuntimeEventType.TURN_FAILED,
            TurnFailedPayload(
                StopReason.CANCELLED,
                error_category,
                "Turn was cancelled during knowledge preparation",
                0,
                0,
            ),
        )
        return TurnResult(StopReason.CANCELLED, "", 0, 0, error_category)

    async def _prepare(
        self, turn_id: str, turn_token: str, query_text: str
    ) -> PreparedTurnKnowledge:
        digest = sha256(query_text.encode("utf-8")).hexdigest()
        catalog_path = self._knowledge_catalog
        backup_path = self._knowledge_backups
        if catalog_path is None or backup_path is None or not catalog_path.is_file():
            return empty_prepared_knowledge(turn_id, turn_token, digest)
        catalog = KnowledgeCatalog(catalog_path, backup_path)
        try:
            catalog.open()
        except KnowledgeError:
            return empty_prepared_knowledge(
                turn_id,
                turn_token,
                digest,
                outcome=RetrievalOutcome.UNAVAILABLE,
            )
        try:
            identity = self._workspace_identity or ""
            bindings = catalog.list_bindings(identity) if identity else ()
            if not bindings:
                return empty_prepared_knowledge(turn_id, turn_token, digest)
            frozen = CatalogBaseResolver(catalog).resolve(bindings)
            indexes: dict[str, KnowledgeIndex] = {}
            source_names: dict[str, str] = {}
            try:
                for binding in bindings:
                    try:
                        for source in catalog.list_sources(binding.knowledge_base_id):
                            source_names[source.source_id] = source.name
                    except KnowledgeError:
                        pass
                    try:
                        kb = catalog.get_knowledge_base(binding.knowledge_base_id)
                        connection = catalog.get_connection(binding.connection_id)
                    except KnowledgeError:
                        continue
                    if connection.endpoint:
                        try:
                            from jdagent.knowledge.milvus import load_milvus_index

                            indexes[kb.knowledge_base_id] = load_milvus_index(connection.endpoint)
                        except KnowledgeError:
                            continue
                        continue
                    if self._knowledge_index is None:
                        continue
                    from jdagent.knowledge.index import FileKnowledgeIndex

                    indexes[kb.knowledge_base_id] = FileKnowledgeIndex(self._knowledge_index)
                preparation = TurnKnowledgePreparation(
                    embedding=AdaptiveEmbedding(),
                    indexes=indexes,
                )
                return await preparation.prepare(
                    turn_id=turn_id,
                    query_text=query_text,
                    bindings=bindings,
                    frozen=frozen,
                    turn_token=turn_token,
                    source_names=source_names,
                )
            finally:
                for index in indexes.values():
                    closer = getattr(index, "close", None)
                    if callable(closer):
                        closer()
        except KnowledgeError:
            return empty_prepared_knowledge(
                turn_id,
                turn_token,
                digest,
                outcome=RetrievalOutcome.UNAVAILABLE,
            )
        finally:
            catalog.close()
