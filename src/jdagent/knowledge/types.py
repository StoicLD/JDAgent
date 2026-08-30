"""Frozen knowledge catalog values shared by use cases and adapters."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from jdagent.domain.json import JsonObject


class ProviderKind(StrEnum):
    JDAGENT_MANAGED = "jdagent_managed"


class KnowledgeBaseStatus(StrEnum):
    ACTIVE = "active"
    DELETING = "deleting"


class SourceLifecycle(StrEnum):
    ACTIVE = "active"
    INACTIVE = "inactive"
    DELETE_PENDING = "delete_pending"
    DELETED = "deleted"


class SagaStage(StrEnum):
    RECEIVED = "received"
    RAW_STORED = "raw_stored"
    PARSED = "parsed"
    INDEXING = "indexing"
    INDEX_VALIDATED = "index_validated"
    ACTIVE = "active"


class OperationKind(StrEnum):
    ADD = "add"
    REPLACE = "replace"
    DEACTIVATE = "deactivate"
    REACTIVATE = "reactivate"
    DELETE = "delete"
    RECONCILE = "reconcile"


@dataclass(frozen=True, slots=True)
class EmbeddingProfile:
    """Versioned document/query vector contract for one knowledge base."""

    provider: str = "openai_compatible"
    base_url: str = ""
    model: str = ""
    dimension: int = 1024
    normalize: bool = True
    timeout_seconds: float = 30.0
    batch_size: int = 32

    def fingerprint(self) -> str:
        payload = json.dumps(
            {
                "base_url": self.base_url,
                "batch_size": self.batch_size,
                "dimension": self.dimension,
                "model": self.model,
                "normalize": self.normalize,
                "provider": self.provider,
                "timeout_seconds": self.timeout_seconds,
            },
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def to_json(self) -> JsonObject:
        return {
            "provider": self.provider,
            "base_url": self.base_url,
            "model": self.model,
            "dimension": self.dimension,
            "normalize": self.normalize,
            "timeout_seconds": self.timeout_seconds,
            "batch_size": self.batch_size,
        }

    @classmethod
    def from_json(cls, data: JsonObject) -> EmbeddingProfile:
        dimension = data.get("dimension", 1024)
        timeout = data.get("timeout_seconds", 30.0)
        batch_size = data.get("batch_size", 32)
        normalize = data.get("normalize", True)
        if not isinstance(dimension, int) or isinstance(dimension, bool):
            raise ValueError("embedding dimension must be an integer")
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)):
            raise ValueError("embedding timeout_seconds must be a number")
        if not isinstance(batch_size, int) or isinstance(batch_size, bool):
            raise ValueError("embedding batch_size must be an integer")
        if not isinstance(normalize, bool):
            raise ValueError("embedding normalize must be a boolean")
        return cls(
            provider=str(data.get("provider", "openai_compatible")),
            base_url=str(data.get("base_url", "")),
            model=str(data.get("model", "")),
            dimension=dimension,
            normalize=normalize,
            timeout_seconds=float(timeout),
            batch_size=batch_size,
        )


@dataclass(frozen=True, slots=True)
class RetrievalProfile:
    """Versioned candidate generation and budget rules."""

    mode: str = "hybrid_rrf"
    dense_top_k: int = 20
    bm25_top_k: int = 20
    fused_child_limit: int = 20
    rerank_pool_size: int = 40
    evidence_token_limit: int = 6000
    parent_limit: int = 8
    parent_token_limit: int = 2000
    rrf_k: int = 60

    def fingerprint(self, embedding_fingerprint: str, language_profile: str) -> str:
        payload = json.dumps(
            {
                "bm25_top_k": self.bm25_top_k,
                "dense_top_k": self.dense_top_k,
                "embedding": embedding_fingerprint,
                "evidence_token_limit": self.evidence_token_limit,
                "fused_child_limit": self.fused_child_limit,
                "language_profile": language_profile,
                "mode": self.mode,
                "parent_limit": self.parent_limit,
                "parent_token_limit": self.parent_token_limit,
                "rerank_pool_size": self.rerank_pool_size,
                "rrf_k": self.rrf_k,
            },
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def to_json(self) -> JsonObject:
        return {
            "mode": self.mode,
            "dense_top_k": self.dense_top_k,
            "bm25_top_k": self.bm25_top_k,
            "fused_child_limit": self.fused_child_limit,
            "rerank_pool_size": self.rerank_pool_size,
            "evidence_token_limit": self.evidence_token_limit,
            "parent_limit": self.parent_limit,
            "parent_token_limit": self.parent_token_limit,
            "rrf_k": self.rrf_k,
        }

    @classmethod
    def from_json(cls, data: JsonObject) -> RetrievalProfile:
        return cls(
            mode=str(data.get("mode", "hybrid_rrf")),
            dense_top_k=_json_int(data, "dense_top_k", 20),
            bm25_top_k=_json_int(data, "bm25_top_k", 20),
            fused_child_limit=_json_int(data, "fused_child_limit", 20),
            rerank_pool_size=_json_int(data, "rerank_pool_size", 40),
            evidence_token_limit=_json_int(data, "evidence_token_limit", 6000),
            parent_limit=_json_int(data, "parent_limit", 8),
            parent_token_limit=_json_int(data, "parent_token_limit", 2000),
            rrf_k=_json_int(data, "rrf_k", 60),
        )


def _json_int(data: JsonObject, name: str, default: int) -> int:
    value = data.get(name, default)
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError(f"{name} must be an integer")
    return value


@dataclass(frozen=True, slots=True)
class CredentialRef:
    kind: str
    value: str

    def redacted(self) -> str:
        if self.kind == "env":
            return f"env:{self.value}"
        return "file:<redacted>"

    def __str__(self) -> str:
        return self.redacted()


@dataclass(frozen=True, slots=True)
class ConnectionRecord:
    connection_id: str
    name: str
    provider_kind: ProviderKind
    endpoint: str | None
    credential_ref: CredentialRef | None
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class KnowledgeBaseRecord:
    knowledge_base_id: str
    connection_id: str
    name: str
    status: KnowledgeBaseStatus
    embedding_profile: EmbeddingProfile
    retrieval_profile: RetrievalProfile
    language_profile: str
    current_generation_id: str | None
    current_revision: int
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class BindingRecord:
    binding_id: str
    workspace_identity: str
    connection_id: str
    knowledge_base_id: str
    created_at: datetime


@dataclass(frozen=True, slots=True)
class SourceRecord:
    source_id: str
    knowledge_base_id: str
    name: str
    lifecycle: SourceLifecycle
    current_version_id: str | None
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class SourceVersionRecord:
    source_version_id: str
    source_id: str
    raw_hash: str
    encoding: str
    encoding_method: str
    snapshot_hash: str
    parser_profile: str
    created_at: datetime


@dataclass(frozen=True, slots=True)
class OperationRecord:
    operation_id: str
    kind: OperationKind
    saga_stage: str
    knowledge_base_id: str
    source_id: str | None
    payload_json: str
    error_code: str | None
    created_at: datetime
    updated_at: datetime
