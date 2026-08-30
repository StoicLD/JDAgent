"""Coordinate one turn without embedding adapter details in the agent loop."""

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol
from uuid import uuid4

from jdagent.core.loop import AgentLoop, CancellationToken, TurnResult
from jdagent.domain.errors import StopReason
from jdagent.domain.events import (
    RetrievalBaseRecord,
    RetrievalBudgetRecord,
    RetrievalEvidenceRecord,
    RuntimeEventType,
    SessionStartedPayload,
    TurnFailedPayload,
    TurnRetrievalRecordedPayload,
    UserMessagePayload,
)
from jdagent.domain.tools import ToolExecutionContext
from jdagent.eventing import EventJournal
from jdagent.knowledge.catalog import KnowledgeCatalog
from jdagent.knowledge.embedding import AdaptiveEmbedding
from jdagent.knowledge.errors import KnowledgeError
from jdagent.knowledge.index import FileKnowledgeIndex, KnowledgeIndex
from jdagent.knowledge.preparation import CatalogBaseResolver, TurnKnowledgePreparation
from jdagent.knowledge.ptk import PreparedTurnKnowledge, empty_prepared_knowledge
from jdagent.observability import TraceProjection
from jdagent.ports import EventObserver, RuntimeJournal, SessionPort


class AgentLoopFactory(Protocol):
    """Create a configured loop for a per-session journal."""

    def create(self, journal: RuntimeJournal) -> AgentLoop: ...


@dataclass(frozen=True, slots=True)
class CoordinatedTurn:
    """Application result with stable IDs and its trace projection."""

    session_id: str
    turn_id: str
    result: TurnResult
    trace: TraceProjection
    knowledge: PreparedTurnKnowledge


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
    ) -> None:
        self._session = session
        self._workspace = workspace
        self._loop_factory = loop_factory
        self._event_observers = event_observers
        self._workspace_identity = workspace_identity
        self._knowledge_catalog = knowledge_catalog
        self._knowledge_backups = knowledge_backups
        self._knowledge_index = knowledge_index

    async def send(
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
        if session_id is not None and new_session_id is not None:
            raise ValueError("session_id and new_session_id are mutually exclusive")
        actual_session_id = session_id or new_session_id or str(uuid4())
        turn_id = str(uuid4())
        turn_token = turn_id.replace("-", "")[:8]
        trace = TraceProjection()
        journal = await EventJournal.open(
            self._session,
            actual_session_id,
            observers=(trace, *self._event_observers),
            require_existing=session_id is not None,
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
        await journal.record(
            turn_id,
            RuntimeEventType.USER_MESSAGE,
            UserMessagePayload(user_text),
        )

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
        result = await loop.run(
            turn_id,
            ToolExecutionContext(actual_session_id, turn_id, self._workspace),
            cancellation=cancellation,
            knowledge=knowledge,
        )
        return CoordinatedTurn(actual_session_id, turn_id, result, trace, knowledge)

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
        catalog_path = self._knowledge_catalog
        backup_path = self._knowledge_backups
        if catalog_path is None or backup_path is None or not catalog_path.is_file():
            return empty_prepared_knowledge(turn_id, turn_token, "")
        catalog = KnowledgeCatalog(catalog_path, backup_path)
        try:
            catalog.open()
        except KnowledgeError:
            return empty_prepared_knowledge(turn_id, turn_token, "")
        try:
            identity = self._workspace_identity or ""
            bindings = catalog.list_bindings(identity) if identity else ()
            if not bindings:
                return empty_prepared_knowledge(turn_id, turn_token, "")
            frozen = CatalogBaseResolver(catalog).resolve(bindings)
            indexes: dict[str, KnowledgeIndex] = {}
            shared: FileKnowledgeIndex | None = None
            for binding in bindings:
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
                if shared is None:
                    shared = FileKnowledgeIndex(self._knowledge_index)
                indexes[kb.knowledge_base_id] = shared
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
            )
        except KnowledgeError:
            return empty_prepared_knowledge(turn_id, turn_token, "")
        finally:
            catalog.close()
