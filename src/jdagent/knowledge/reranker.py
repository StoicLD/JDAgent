"""定义跨知识库的重排接口；重排失败时使用先前的顺序。"""

from __future__ import annotations

from typing import Protocol

from jdagent.knowledge.index import IndexHit


class RerankerPort(Protocol):
    """约定如何为已合并的候选重新排序，供检索流程按需替换实现。"""

    def rerank(self, query_text: str, hits: tuple[IndexHit, ...]) -> tuple[IndexHit, ...]: ...


class IdentityReranker:
    """保留输入顺序，用于验证未调整排名时的检索流程。"""

    def rerank(self, query_text: str, hits: tuple[IndexHit, ...]) -> tuple[IndexHit, ...]:
        del query_text
        return hits


class ExplodingReranker:
    def rerank(self, query_text: str, hits: tuple[IndexHit, ...]) -> tuple[IndexHit, ...]:
        del query_text, hits
        raise RuntimeError("reranker unavailable")
