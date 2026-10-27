"""混合召回测试：RRF 融合、三档判定、降级链、LLM 重排回退。

这些用例锁定 Spec §7.2 的行为契约，尤其是"低置信禁止生成"。
"""

from __future__ import annotations

import json
from collections.abc import Sequence

import pytest

from app.common.config import Settings
from app.llm.embeddings import HashEmbedding
from app.llm.types import ChatResult, LLMError
from app.rag.chroma import VectorHit
from app.rag.errors import RetrievalError
from app.rag.keyword import KeywordDocument, KeywordHit, MemoryBM25KeywordIndex
from app.rag.pipeline import (
    HybridRetriever,
    LlmReranker,
    NoopReranker,
    RetrievalHit,
    fuse_hits,
)
from app.rag.retriever import ConfidenceBand


def make_settings(**overrides: object) -> Settings:
    base: dict[str, object] = {
        "_env_file": None,
        "app_env": "test",
        "rag_top1_threshold": 0.75,
        "rag_low_threshold": 0.55,
        "rag_top_k_vector": 5,
        "rag_top_k_keyword": 5,
        "rag_top_k_fused": 4,
        "rag_top_k_final": 3,
        "rag_rrf_k": 60,
    }
    base.update(overrides)
    return Settings(**base)


def vector_hit(chunk_id: str, score: float, *, section: str = "节") -> VectorHit:
    return VectorHit(
        chunk_id=chunk_id,
        doc_id=chunk_id.split("#", 1)[0],
        title="售后政策",
        section=section,
        content=f"{section}正文",
        score=score,
        distance=1 - score,
    )


class FakeVectorIndex:
    def __init__(
        self, hits: Sequence[VectorHit] | None = None, error: Exception | None = None
    ) -> None:
        self._hits = list(hits or [])
        self._error = error
        self.queries: list[str] = []

    async def create_collection(self, name: str) -> str:
        return f"fake-{name}"

    async def upsert(self, *args: object, **kwargs: object) -> None:
        return None

    async def search(
        self, collection_id: str, embedding: list[float], *, top_k: int
    ) -> list[VectorHit]:
        self.queries.append(collection_id)
        if self._error is not None:
            raise self._error
        return self._hits[:top_k]

    async def delete_collection(self, name: str) -> None:
        return None


class FailingKeywordIndex:
    async def search(self, query: str, *, index_version: str, top_k: int) -> list[KeywordHit]:
        raise RetrievalError("mysql down")


def keyword_hit(chunk_id: str, score: float) -> KeywordHit:
    return KeywordHit(
        chunk_id=chunk_id,
        doc_id=chunk_id.split("#", 1)[0],
        title="售后政策",
        section="节",
        content="正文",
        score=score,
    )


def test_fuse_prefers_document_hit_by_both_channels() -> None:
    fused = fuse_hits(
        [vector_hit("a#0", 0.8), vector_hit("b#0", 0.7)],
        [keyword_hit("c#0", 9.0), keyword_hit("a#0", 5.0)],
        rrf_k=60,
        top_n=3,
    )
    assert next(hit.chunk_id for hit in fused) == "a#0"
    assert fused[0].vector_rank == 1
    assert fused[0].keyword_rank == 2
    assert fused[0].score == pytest.approx(0.8)  # 判定分用向量分


def test_fuse_carries_keyword_only_hits_with_caveat_score() -> None:
    fused = fuse_hits([], [keyword_hit("q#0", 3.0)], rrf_k=60, top_n=3)
    assert len(fused) == 1
    assert fused[0].vector_score is None
    assert fused[0].keyword_score == pytest.approx(1.0)
    assert fused[0].score < 0.75  # 关键词分按 0.6 折减，必然进"带提示"或兜底


async def test_retriever_high_confidence_answers() -> None:
    retriever = HybridRetriever(
        embedding=HashEmbedding(dim=32),
        vector_index=FakeVectorIndex([vector_hit("a#0", 0.9)]),
        keyword_index=MemoryBM25KeywordIndex(),
        settings=make_settings(),
    )
    result = await retriever.retrieve("退款多久到账", index_version="v1", collection_id="c1")
    assert result.band is ConfidenceBand.ANSWER
    assert result.top1_score == pytest.approx(0.9)
    assert result.hits
    assert result.degraded == []


async def test_retriever_low_confidence_falls_back() -> None:
    retriever = HybridRetriever(
        embedding=HashEmbedding(dim=32),
        vector_index=FakeVectorIndex([vector_hit("a#0", 0.3)]),
        keyword_index=MemoryBM25KeywordIndex(),
        settings=make_settings(),
    )
    result = await retriever.retrieve("无关问题", index_version="v1", collection_id="c1")
    assert result.band is ConfidenceBand.FALLBACK


async def test_retriever_mid_confidence_adds_caveat() -> None:
    retriever = HybridRetriever(
        embedding=HashEmbedding(dim=32),
        vector_index=FakeVectorIndex([vector_hit("a#0", 0.62)]),
        keyword_index=MemoryBM25KeywordIndex(),
        settings=make_settings(),
    )
    result = await retriever.retrieve("退款", index_version="v1", collection_id="c1")
    assert result.band is ConfidenceBand.ANSWER_WITH_CAVEAT


async def test_retriever_without_collection_uses_keyword_only() -> None:
    keyword_index = MemoryBM25KeywordIndex()
    keyword_index.add_documents(
        "v1",
        [
            KeywordDocument(
                chunk_id="faq#0",
                doc_id="faq",
                title="常见问题",
                section="退款",
                content="退款多久到账？余额 1 个工作日。",
            )
        ],
    )
    retriever = HybridRetriever(
        embedding=HashEmbedding(dim=32),
        vector_index=FakeVectorIndex([]),
        keyword_index=keyword_index,
        settings=make_settings(),
    )
    result = await retriever.retrieve("退款多久到账", index_version="v1", collection_id=None)
    assert result.vector_ok is False
    assert "vector:no_collection" in result.degraded
    assert result.band is ConfidenceBand.ANSWER_WITH_CAVEAT
    assert result.hits[0].keyword_rank == 1


async def test_retriever_degrades_when_vector_errors() -> None:
    keyword_index = MemoryBM25KeywordIndex()
    keyword_index.add_documents(
        "v1",
        [
            KeywordDocument(
                chunk_id="faq#0",
                doc_id="faq",
                title="FAQ",
                section="退款",
                content="退款到账时间说明",
            )
        ],
    )
    retriever = HybridRetriever(
        embedding=HashEmbedding(dim=32),
        vector_index=FakeVectorIndex(error=RetrievalError("chroma down")),
        keyword_index=keyword_index,
        settings=make_settings(),
    )
    result = await retriever.retrieve("退款到账", index_version="v1", collection_id="c1")
    assert result.vector_ok is False
    assert any(item.startswith("vector:") for item in result.degraded)
    assert result.hits


async def test_retriever_degrades_when_keyword_errors() -> None:
    retriever = HybridRetriever(
        embedding=HashEmbedding(dim=32),
        vector_index=FakeVectorIndex([vector_hit("a#0", 0.9)]),
        keyword_index=FailingKeywordIndex(),
        settings=make_settings(),
    )
    result = await retriever.retrieve("退款", index_version="v1", collection_id="c1")
    assert result.keyword_ok is False
    assert any(item.startswith("keyword:") for item in result.degraded)
    assert result.band is ConfidenceBand.ANSWER


async def test_retriever_total_miss_is_fallback() -> None:
    retriever = HybridRetriever(
        embedding=HashEmbedding(dim=32),
        vector_index=FakeVectorIndex([]),
        keyword_index=MemoryBM25KeywordIndex(),
        settings=make_settings(),
    )
    result = await retriever.retrieve("完全无关", index_version="v1", collection_id="c1")
    assert result.empty
    assert result.band is ConfidenceBand.FALLBACK


def test_retrieval_hit_citation_shape() -> None:
    hit = fuse_hits([vector_hit("a#0", 0.9)], [], rrf_k=60, top_n=3)[0]
    citation = hit.as_citation(1)
    assert citation["id"] == 1
    assert citation["doc_id"] == "a"
    assert citation["title"] == "售后政策"
    assert set(citation) == {"id", "doc_id", "title", "section", "snippet", "score"}


class FakeChat:
    def __init__(self, content: str | None = None, error: Exception | None = None) -> None:
        self._content = content
        self._error = error
        self.calls = 0

    async def complete(self, messages: object, **kwargs: object) -> ChatResult:
        self.calls += 1
        if self._error is not None:
            raise self._error
        return ChatResult(
            content=self._content or "{}",
            model="fake",
            usage=__import__("app.llm.types", fromlist=["Usage"]).Usage(),
        )

    async def stream(self, *args: object, **kwargs: object) -> object:  # pragma: no cover
        raise NotImplementedError

    async def aclose(self) -> None:
        return None


async def test_llm_reranker_reorders_by_model_scores() -> None:
    hits = fuse_hits(
        [vector_hit("a#0", 0.9), vector_hit("b#0", 0.8)],
        [],
        rrf_k=60,
        top_n=4,
    )
    chat = FakeChat(
        content=json.dumps({"scores": [{"id": 1, "score": 0.2}, {"id": 2, "score": 0.9}]})
    )
    reranked = await LlmReranker(chat).rerank("问题", hits, top_k=2)  # type: ignore[arg-type]
    assert [hit.chunk_id for hit in reranked] == ["b#0", "a#0"]
    assert reranked[0].score == pytest.approx(0.9)
    assert reranked[0].rerank_score == pytest.approx(0.9)


async def test_llm_reranker_falls_back_on_bad_json() -> None:
    hits = fuse_hits([vector_hit("a#0", 0.9), vector_hit("b#0", 0.8)], [], rrf_k=60, top_n=4)
    chat = FakeChat(content="not-json")
    reranked = await LlmReranker(chat).rerank("问题", hits, top_k=2)  # type: ignore[arg-type]
    assert [hit.chunk_id for hit in reranked] == ["a#0", "b#0"]


async def test_llm_reranker_falls_back_on_llm_error() -> None:
    hits = fuse_hits([vector_hit("a#0", 0.9)], [], rrf_k=60, top_n=4)
    chat = FakeChat(error=LLMError("boom"))
    reranked = await LlmReranker(chat).rerank("问题", hits, top_k=2)  # type: ignore[arg-type]
    assert [hit.chunk_id for hit in reranked] == ["a#0"]


async def test_noop_reranker_sorts_by_score_then_truncates() -> None:
    hits = [
        RetrievalHit(
            chunk_id=f"d{i}#0",
            doc_id=f"d{i}",
            title="t",
            section="s",
            content="c",
            score=1.0 - i * 0.1,
        )
        for i in range(5)
    ]
    result = await NoopReranker().rerank("q", hits, top_k=2)
    assert [hit.chunk_id for hit in result] == ["d0#0", "d1#0"]


async def test_noop_reranker_demotes_high_rrf_but_low_vector_score() -> None:
    """RRF 名次靠前但向量分更低时，最终排序应按向量分（否则三档判定的 top1 失真）。"""
    fused_first = RetrievalHit(
        chunk_id="rrf-first#0",
        doc_id="rrf-first",
        title="t",
        section="s",
        content="c",
        score=0.70,
        fused_score=0.032,
    )
    better_evidence = RetrievalHit(
        chunk_id="better#0",
        doc_id="better",
        title="t",
        section="s",
        content="c",
        score=0.88,
        fused_score=0.021,
    )
    result = await NoopReranker().rerank("q", [fused_first, better_evidence], top_k=2)
    assert [hit.chunk_id for hit in result] == ["better#0", "rrf-first#0"]


def test_render_result_json_and_text() -> None:
    from app.rag.cli import render_result
    from app.rag.pipeline import RetrievalResult

    hit = fuse_hits([vector_hit("a#0", 0.81)], [], rrf_k=60, top_n=3)[0]
    result = RetrievalResult(
        query="退款多久到账",
        index_version="v1",
        hits=[hit],
        band=ConfidenceBand.ANSWER,
        degraded=["keyword:RetrievalError"],
    )
    payload = json.loads(render_result(result, as_json=True))
    assert payload["band"] == "answer"
    assert payload["top1_score"] == pytest.approx(0.81)
    assert payload["degraded"] == ["keyword:RetrievalError"]
    assert payload["hits"][0]["rank"] == 1

    text = render_result(result, as_json=False)
    assert "band       : answer" in text
    assert "退款多久到账" in text
    assert "[1] 0.8100" in text

    empty = RetrievalResult(query="q", index_version="v1", hits=[], band=ConfidenceBand.FALLBACK)
    assert "（无命中）" in render_result(empty)
