"""将文本转换成向量，统一校验各实现返回的数量、维数和数值。"""

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
    """区分用于建索引的文档和用于搜索的查询，使向量服务可以分别处理两类输入。"""

    DOCUMENT = "document"
    QUERY = "query"


@dataclass(frozen=True, slots=True)
class EmbeddingResult:
    """按输入顺序保存向量，同时记录生成向量的模型标识和向量维数。"""

    vectors: tuple[tuple[float, ...], ...]
    model_identity: str
    dimension: int


class EmbeddingPort(Protocol):
    """约定如何将一批文档或查询文本转换成可供索引使用的向量。"""

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
    """在写入或检索之前校验向量数量、维度及数值，防止不同规格的数据混入索引。"""
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
    """用确定性的散列值为文本生成向量，供离线摄取和测试重现相同结果。"""

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
    """通过兼容的 HTTP embeddings 接口生成向量，并校验返回结果是否与请求配置一致。"""

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
        """按配置分批发送文本，校验返回的向量，并按原输入顺序合并结果。"""
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
                batch_vectors = await self._embed_batch(client, batch, profile)
                validate_vectors(batch, batch_vectors, profile)
                if profile.normalize:
                    normalized: list[tuple[float, ...]] = []
                    for vector in batch_vectors:
                        norm = math.hypot(*vector)
                        if norm == 0 or not math.isfinite(norm):
                            raise KnowledgeError(
                                KnowledgeErrorCode.PROVIDER_UNAVAILABLE,
                                "Embedding vector cannot be normalized",
                            )
                        normalized.append(tuple(value / norm for value in vector))
                    batch_vectors = tuple(normalized)
                vectors.extend(batch_vectors)
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
        """发送一批文本并按返回的索引还原向量顺序，使每个结果与原输入对应。"""
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
        indexed: dict[int, object] = {}
        if any(isinstance(item, dict) and "index" in item for item in entries):
            for item in entries:
                position = (
                    cast(dict[str, object], item).get("index") if isinstance(item, dict) else None
                )
                if (
                    isinstance(position, bool)
                    or not isinstance(position, int)
                    or not 0 <= position < len(texts)
                    or position in indexed
                ):
                    raise KnowledgeError(
                        KnowledgeErrorCode.PROVIDER_UNAVAILABLE,
                        "Embedding response index is invalid",
                    )
                indexed[position] = item
            if len(indexed) != len(texts):
                raise KnowledgeError(
                    KnowledgeErrorCode.PROVIDER_UNAVAILABLE,
                    "Embedding response indexes are incomplete",
                )
            entries = [indexed[position] for position in range(len(texts))]
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
    """按知识库的向量配置选择 HTTP 服务或本地散列向量实现。"""

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
