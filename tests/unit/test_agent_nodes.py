"""节点纯逻辑测试：规则兜底、路由、历史渲染、引用工具。"""

from __future__ import annotations

import pytest

from app.agent.citations import (
    build_context_blocks,
    citation_for_marker,
    has_citation,
    referenced_ids,
    to_citations,
)
from app.agent.nodes.core import (
    CHITCHAT_TEXT,
    NO_ANSWER_TEXT,
    PENDING_TEXT,
    classify_by_rules,
    history_text,
    route_after_retrieve,
    route_after_router,
)
from app.agent.state import Intent
from app.rag.pipeline import RetrievalHit


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("七天无理由怎么算时间", Intent.QA),
        ("退款多久到账", Intent.QA),
        ("我的退款到哪了", Intent.QUERY),
        ("物流一直不更新", Intent.QUERY),
        ("我要退这个耳机", Intent.AFTER_SALES),
        ("帮我申请售后退货", Intent.AFTER_SALES),
        ("你好", Intent.CHITCHAT),
        ("今天天气怎么样", Intent.QA),  # 规则兜底宁可走检索，也不误判办理
    ],
)
def test_classify_by_rules(text: str, expected: Intent) -> None:
    intent, confidence = classify_by_rules(text)
    assert intent is expected
    assert 0.0 < confidence <= 1.0


def test_classify_by_rules_empty() -> None:
    intent, confidence = classify_by_rules("   ")
    assert intent is Intent.CHITCHAT
    assert confidence < 0.5


def test_history_text_renders_last_turns() -> None:
    history = [
        {"role": "user", "content": "问题1"},
        {"role": "assistant", "content": "回答1"},
        {"role": "user", "content": "问题2"},
    ]
    rendered = history_text(history)
    assert "user: 问题1" in rendered
    assert "assistant: 回答1" in rendered
    assert history_text([]) == "（无历史）"


def test_history_text_caps_turns() -> None:
    history = [{"role": "user", "content": f"q{i}"} for i in range(20)]
    rendered = history_text(history, max_turns=4)
    assert "q19" in rendered
    assert "q0" not in rendered


def test_route_after_router() -> None:
    assert route_after_router({"intent": Intent.QA}) == "rewrite"
    assert route_after_router({"intent": Intent.QUERY}) == "pending_capability"
    assert route_after_router({"intent": Intent.AFTER_SALES}) == "pending_capability"
    assert route_after_router({"intent": Intent.CHITCHAT}) == "reject"
    assert route_after_router({}) == "rewrite"  # 缺省按 qa 处理


def test_route_after_retrieve() -> None:
    assert route_after_retrieve({"band": "fallback"}) == "clarify"
    assert route_after_retrieve({"band": "answer"}) == "answer"
    assert route_after_retrieve({}) == "answer"


def test_static_texts_are_non_empty_and_distinct() -> None:
    assert len({NO_ANSWER_TEXT, CHITCHAT_TEXT, PENDING_TEXT}) == 3
    assert "人工" in NO_ANSWER_TEXT
    assert "售后" in CHITCHAT_TEXT
    assert "M2" in PENDING_TEXT


def _hit(chunk_id: str = "policy#0", score: float = 0.9) -> RetrievalHit:
    return RetrievalHit(
        chunk_id=chunk_id,
        doc_id="policy",
        title="售后政策总则",
        section="退款时效",
        content="退货退款在商家签收后原路退回，余额 1 个工作日内到账。",
        score=score,
    )


def test_build_context_blocks_and_citations_align() -> None:
    hits = [_hit("a#0"), _hit("b#1")]
    blocks = build_context_blocks(hits)
    citations = to_citations(hits)
    assert blocks[0].startswith("[1] 售后政策总则 / 退款时效")
    assert citations[0].id == 1
    assert citations[1].id == 2
    assert citation_for_marker(citations, 2) is citations[1]
    assert citation_for_marker(citations, 9) is None


def test_citation_marker_parsing() -> None:
    text = "有效期七天[[1]]，运费[[2]]，再提一次[[1]]。"
    assert referenced_ids(text) == [1, 2]
    assert has_citation(text) is True
    assert has_citation("没有标记") is False
    assert referenced_ids("") == []
