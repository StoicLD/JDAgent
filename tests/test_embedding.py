import asyncio
import json
import math

import httpx
import pytest

from jdagent.knowledge.embedding import (
    EmbeddingKind,
    HashEmbedding,
    OpenAICompatibleEmbedding,
    hash_vector,
    validate_vectors,
)
from jdagent.knowledge.errors import KnowledgeError, KnowledgeErrorCode
from jdagent.knowledge.types import EmbeddingProfile


def test_hash_embedding_is_deterministic_and_validates_shape() -> None:
    async def scenario() -> None:
        profile = EmbeddingProfile(dimension=8, normalize=True)
        embedding = HashEmbedding()
        first = await embedding.embed(("hello",), profile, input_kind=EmbeddingKind.QUERY)
        second = await embedding.embed(("hello",), profile, input_kind=EmbeddingKind.DOCUMENT)
        assert first.vectors == second.vectors
        assert first.dimension == 8
        norm = math.sqrt(sum(value * value for value in first.vectors[0]))
        assert abs(norm - 1.0) < 1e-6
        with pytest.raises(KnowledgeError) as error:
            validate_vectors(("a",), ((1.0, float("nan")),), EmbeddingProfile(dimension=2))
        assert error.value.code is KnowledgeErrorCode.PROVIDER_UNAVAILABLE

    asyncio.run(scenario())


def test_http_embedding_retries_then_validates() -> None:
    async def scenario() -> None:
        calls = {"count": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            assert request.headers.get("Authorization") == "Bearer secret-token"
            calls["count"] += 1
            if calls["count"] < 2:
                return httpx.Response(429, json={"error": "rate"})
            return httpx.Response(
                200,
                json={"data": [{"embedding": [0.0, 1.0, 0.0, 0.0]}]},
            )

        transport = httpx.MockTransport(handler)
        client = httpx.AsyncClient(transport=transport, base_url="https://embed.test")
        adapter = OpenAICompatibleEmbedding(client, api_key="secret-token")
        profile = EmbeddingProfile(
            base_url="https://embed.test",
            model="demo",
            dimension=4,
            normalize=False,
        )
        result = await adapter.embed(("query",), profile, input_kind=EmbeddingKind.QUERY)
        assert result.vectors[0] == (0.0, 1.0, 0.0, 0.0)
        assert calls["count"] == 2
        await client.aclose()
        assert hash_vector("query", 4, normalize=False)

    asyncio.run(scenario())


def test_http_embedding_honors_profile_batch_size() -> None:
    async def scenario() -> None:
        seen: list[int] = []

        def handler(request: httpx.Request) -> httpx.Response:
            payload = json.loads(request.content.decode("utf-8"))
            seen.append(len(payload["input"]))
            vectors = [[0.0, 1.0, 0.0, 0.0] for _ in payload["input"]]
            return httpx.Response(200, json={"data": [{"embedding": item} for item in vectors]})

        transport = httpx.MockTransport(handler)
        client = httpx.AsyncClient(transport=transport, base_url="https://embed.test")
        adapter = OpenAICompatibleEmbedding(client)
        profile = EmbeddingProfile(
            base_url="https://embed.test",
            model="demo",
            dimension=4,
            normalize=False,
            batch_size=2,
        )
        result = await adapter.embed(
            ("a", "b", "c"),
            profile,
            input_kind=EmbeddingKind.DOCUMENT,
        )
        assert seen == [2, 1]
        assert len(result.vectors) == 3
        await client.aclose()

    asyncio.run(scenario())
