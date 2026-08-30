"""Embedding adapters that share one request and validation path."""

from __future__ import annotations

import asyncio
import hashlib
import math
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol, cast

import httpx

from jdagent.knowledge.errors import KnowledgeError, KnowledgeErrorCode
from jdagent.knowledge.types import EmbeddingProfile


class EmbeddingKind(StrEnum):
    DOCUMENT = "document"
    QUERY = "query"


@dataclass(frozen=True, slots=True)
class EmbeddingResult:
    vectors: tuple[tuple[float, ...], ...]
    model_identity: str
    dimension: int


class EmbeddingPort(Protocol):
    async def embed(
        self,
        texts: tuple[str, ...],
        profile: EmbeddingProfile,
        *,
        input_kind: EmbeddingKind,
    ) -> EmbeddingResult: ...


def validate_vectors(
    texts: tuple[str, ...],
    vectors: tuple[tuple[float, ...], ...],
    profile: EmbeddingProfile,
) -> None:
    if len(vectors) != len(texts):
        raise KnowledgeError(
            KnowledgeErrorCode.PROVIDER_UNAVAILABLE,
            "Embedding count does not match input texts",
        )
    for vector in vectors:
        if len(vector) != profile.dimension:
            raise KnowledgeError(
                KnowledgeErrorCode.PROVIDER_UNAVAILABLE,
                "Embedding dimension does not match profile",
            )
        if any(not math.isfinite(value) for value in vector):
            raise KnowledgeError(
                KnowledgeErrorCode.PROVIDER_UNAVAILABLE,
                "Embedding contains a non-finite value",
            )


def hash_vector(text: str, dimension: int, *, normalize: bool) -> tuple[float, ...]:
    digest = hashlib.sha256(text.encode("utf-8")).digest()
    values: list[float] = []
    seed = digest
    while len(values) < dimension:
        for byte in seed:
            values.append((byte / 127.5) - 1.0)
            if len(values) >= dimension:
                break
        seed = hashlib.sha256(seed).digest()
    if normalize:
        norm = math.sqrt(sum(value * value for value in values)) or 1.0
        values = [value / norm for value in values]
    return tuple(values[:dimension])


class HashEmbedding:
    """Deterministic embedding used by tests and offline ingest."""

    async def embed(
        self,
        texts: tuple[str, ...],
        profile: EmbeddingProfile,
        *,
        input_kind: EmbeddingKind,
    ) -> EmbeddingResult:
        del input_kind
        vectors = tuple(
            hash_vector(text, profile.dimension, normalize=profile.normalize) for text in texts
        )
        validate_vectors(texts, vectors, profile)
        return EmbeddingResult(vectors, f"hash:{profile.model or 'local'}", profile.dimension)


class OpenAICompatibleEmbedding:
    """OpenAI-compatible HTTP /embeddings adapter."""

    def __init__(self, client: httpx.AsyncClient | None = None, api_key: str | None = None) -> None:
        self._client = client
        self._api_key = api_key

    async def embed(
        self,
        texts: tuple[str, ...],
        profile: EmbeddingProfile,
        *,
        input_kind: EmbeddingKind,
    ) -> EmbeddingResult:
        del input_kind
        if not profile.base_url or not profile.model:
            raise KnowledgeError(
                KnowledgeErrorCode.ACCESS_DENIED,
                "Embedding profile is missing base_url or model",
            )
        client = self._client or httpx.AsyncClient(base_url=profile.base_url)
        owns_client = self._client is None
        batch_size = max(1, profile.batch_size)
        vectors: list[tuple[float, ...]] = []
        try:
            for start in range(0, len(texts), batch_size):
                batch = texts[start : start + batch_size]
                vectors.extend(await self._embed_batch(client, batch, profile))
        finally:
            if owns_client:
                await client.aclose()
        result = tuple(vectors)
        validate_vectors(texts, result, profile)
        return EmbeddingResult(result, profile.model, profile.dimension)

    async def _embed_batch(
        self,
        client: httpx.AsyncClient,
        texts: tuple[str, ...],
        profile: EmbeddingProfile,
    ) -> tuple[tuple[float, ...], ...]:
        attempts = 0
        last_error: Exception | None = None
        payload: object = None
        while attempts < 3:
            attempts += 1
            try:
                headers: dict[str, str] = {}
                if self._api_key:
                    headers["Authorization"] = f"Bearer {self._api_key}"
                response = await client.post(
                    "/embeddings",
                    json={"model": profile.model, "input": list(texts)},
                    timeout=profile.timeout_seconds,
                    headers=headers,
                )
                if response.status_code in {429, 502, 503} and attempts < 3:
                    continue
                response.raise_for_status()
                payload = response.json()
                break
            except (httpx.HTTPError, ValueError, KeyError) as error:
                last_error = error
                if attempts >= 3:
                    raise KnowledgeError(
                        KnowledgeErrorCode.PROVIDER_UNAVAILABLE,
                        "Embedding provider request failed",
                    ) from error
                await asyncio.sleep(0)
        else:
            raise KnowledgeError(
                KnowledgeErrorCode.PROVIDER_UNAVAILABLE,
                "Embedding provider request failed",
            ) from last_error
        if not isinstance(payload, dict):
            raise KnowledgeError(
                KnowledgeErrorCode.PROVIDER_UNAVAILABLE,
                "Embedding response is missing data",
            )
        data = cast(dict[str, object], payload)
        items = data.get("data")
        if not isinstance(items, list):
            raise KnowledgeError(
                KnowledgeErrorCode.PROVIDER_UNAVAILABLE,
                "Embedding response is missing data",
            )
        entries = cast(list[object], items)
        vectors: list[tuple[float, ...]] = []
        for item in entries:
            if not isinstance(item, dict):
                raise KnowledgeError(
                    KnowledgeErrorCode.PROVIDER_UNAVAILABLE,
                    "Embedding response item is invalid",
                )
            entry = cast(dict[str, object], item)
            raw = entry.get("embedding")
            if not isinstance(raw, list):
                raise KnowledgeError(
                    KnowledgeErrorCode.PROVIDER_UNAVAILABLE,
                    "Embedding vector is missing",
                )
            parsed: list[float] = []
            for value in cast(list[object], raw):
                if isinstance(value, bool) or not isinstance(value, (int, float)):
                    raise KnowledgeError(
                        KnowledgeErrorCode.PROVIDER_UNAVAILABLE,
                        "Embedding vector is missing",
                    )
                parsed.append(float(value))
            vectors.append(tuple(parsed))
        return tuple(vectors)


class AdaptiveEmbedding:
    """Use HTTP embeddings when a profile names a provider; otherwise hash."""

    def __init__(self, api_key: str | None = None) -> None:
        self._hash = HashEmbedding()
        self._http = OpenAICompatibleEmbedding(api_key=api_key)

    async def embed(
        self,
        texts: tuple[str, ...],
        profile: EmbeddingProfile,
        *,
        input_kind: EmbeddingKind,
    ) -> EmbeddingResult:
        if profile.base_url and profile.model:
            return await self._http.embed(texts, profile, input_kind=input_kind)
        return await self._hash.embed(texts, profile, input_kind=input_kind)
