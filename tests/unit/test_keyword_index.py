"""关键词通道测试：分词、BM25 排序、分数归一化（MySQL 实现由集成测试覆盖）。"""

from __future__ import annotations

import pytest

from app.rag.keyword import (
    EmptyKeywordIndex,
    KeywordDocument,
    MemoryBM25KeywordIndex,
    chunk_id_of,
    normalize_for_keyword,
    normalize_scores,
    tokenize,
)


def _document(doc_id: str, index: int, title: str, section: str, content: str) -> KeywordDocument:
    return KeywordDocument(
        chunk_id=chunk_id_of(doc_id, index),
        doc_id=doc_id,
        title=title,
        section=section,
        content=content,
    )


def test_chunk_id_convention() -> None:
    assert chunk_id_of("售后政策总则", 0) == "售后政策总则#0"
    assert chunk_id_of("售后政策总则", 12) == "售后政策总则#12"


def test_normalize_for_keyword_strips_punctuation() -> None:
    assert normalize_for_keyword("退款，多久到账？") == "退款 多久到账"


def test_tokenize_emits_latin_words_and_cjk_ngrams() -> None:
    tokens = tokenize("退款 refund 到账")
    assert "refund" in tokens
    assert "退款" in tokens
    assert "退" in tokens


def test_tokenize_empty() -> None:
    assert tokenize("   ") == []
    assert tokenize("!!!") == []


async def test_bm25_ranks_relevant_document_first() -> None:
    index = MemoryBM25KeywordIndex()
    index.add_documents(
        "v1",
        [
            _document(
                "policy", 0, "售后政策", "七天无理由", "自签收之日起七日内可申请无理由退货。"
            ),
            _document("policy", 1, "售后政策", "运费", "无理由退货的寄回运费由消费者承担。"),
            _document("faq", 0, "常见问题", "保修", "整机保修一年，主要部件保修两年。"),
        ],
    )
    hits = await index.search("退货运费谁承担", index_version="v1", top_k=3)
    assert hits
    assert hits[0].section == "运费"
    assert hits[0].chunk_id == "policy#1"
    assert hits[0].score > 0


async def test_bm25_respects_top_k_and_unknown_version() -> None:
    index = MemoryBM25KeywordIndex()
    index.add_documents(
        "v1",
        [
            _document("a", 0, "标题", "节", "退款到账时间说明"),
            _document("b", 0, "标题", "节", "退款失败处理方式"),
        ],
    )
    hits = await index.search("退款", index_version="v1", top_k=1)
    assert len(hits) == 1
    assert await index.search("退款", index_version="v2", top_k=5) == []


async def test_bm25_empty_query_returns_nothing() -> None:
    index = MemoryBM25KeywordIndex()
    index.add_documents("v1", [_document("a", 0, "t", "s", "内容")])
    assert await index.search("，。", index_version="v1", top_k=5) == []


async def test_empty_index_implementation() -> None:
    assert await EmptyKeywordIndex().search("退款", index_version="v1", top_k=5) == []


def test_normalize_scores_scales_to_one() -> None:
    from app.rag.keyword import KeywordHit

    hits = [
        KeywordHit(chunk_id="a#0", doc_id="a", title="t", section="s", content="c", score=4.0),
        KeywordHit(chunk_id="b#0", doc_id="b", title="t", section="s", content="c", score=2.0),
    ]
    normalized = normalize_scores(hits)
    assert normalized[0].score == pytest.approx(1.0)
    assert normalized[1].score == pytest.approx(0.5)
    assert normalize_scores([]) == []
