"""Optional global reranker seam. Failure falls back to the pre-rerank order."""

from __future__ import annotations

from typing import Protocol

from jdagent.knowledge.index import IndexHit


class RerankerPort(Protocol):
    def rerank(self, query_text: str, hits: tuple[IndexHit, ...]) -> tuple[IndexHit, ...]: ...


class IdentityReranker:
    def rerank(self, query_text: str, hits: tuple[IndexHit, ...]) -> tuple[IndexHit, ...]:
        del query_text
        return hits


class ExplodingReranker:
    def rerank(self, query_text: str, hits: tuple[IndexHit, ...]) -> tuple[IndexHit, ...]:
        del query_text, hits
        raise RuntimeError("reranker unavailable")
