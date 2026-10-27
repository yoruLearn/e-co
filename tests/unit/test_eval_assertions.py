"""评测断言与归因测试（Spec §13.3）。"""

from __future__ import annotations

import pytest

from app.eval.assertions import check_expectations


def values(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "intent": "qa",
        "band": "answer",
        "answer_stream": "有效期七天[[1]]，需商品完好。",
        "citations": [
            {
                "doc_id": "policy",
                "title": "政策",
                "section": "七天无理由",
                "snippet": "签收次日起七日内",
            }
        ],
        "rewritten_query": "退款多久到账",
    }
    base.update(overrides)
    return base


def test_all_dimensions_pass() -> None:
    outcome = check_expectations(
        expected={
            "intent": "qa",
            "keywords": ["七天", "完好"],
            "must_cite": True,
            "min_citations": 1,
        },
        values=values(),
        user_query="退款多久到账",
    )
    assert outcome.passed is True
    assert outcome.failures == []
    assert outcome.attribution is None


def test_intent_mismatch_attributes_router() -> None:
    outcome = check_expectations(expected={"intent": "query"}, values=values(), user_query="q")
    assert outcome.passed is False
    assert outcome.attribution == "router"
    assert "意图不符" in outcome.failures[0]


def test_missing_keyword_attributes_answer() -> None:
    outcome = check_expectations(
        expected={"keywords": ["不存在的词"]}, values=values(), user_query="q"
    )
    assert outcome.attribution == "answer"


def test_missing_citation_without_hits_attributes_retrieve() -> None:
    outcome = check_expectations(
        expected={"must_cite": True},
        values=values(citations=[], answer_stream="没有引用的回答。"),
        user_query="q",
    )
    assert outcome.passed is False
    assert outcome.attribution == "retrieve"


def test_missing_citation_with_hits_attributes_answer() -> None:
    outcome = check_expectations(
        expected={"must_cite": True},
        values=values(answer_stream="有资料但没标引用。"),
        user_query="q",
    )
    assert outcome.attribution == "answer"


def test_min_citations_enforced() -> None:
    outcome = check_expectations(expected={"min_citations": 2}, values=values(), user_query="q")
    assert outcome.passed is False
    assert "引用数不足" in outcome.failures[0]


def test_must_fallback_passes_on_low_confidence() -> None:
    outcome = check_expectations(
        expected={"must_fallback": True},
        values=values(
            band="fallback",
            citations=[],
            answer_stream="抱歉，我暂时没有找到足够准确的售后政策依据，请补充商品名称。",
        ),
        user_query="q",
    )
    assert outcome.passed is True


def test_must_fallback_fails_on_confident_answer() -> None:
    outcome = check_expectations(expected={"must_fallback": True}, values=values(), user_query="q")
    assert outcome.passed is False
    assert "未走兜底" in outcome.failures[0]


def test_must_refuse_passes_for_chitchat_reply() -> None:
    outcome = check_expectations(
        expected={"must_refuse": True},
        values=values(
            intent="chitchat",
            citations=[],
            band="answer",
            answer_stream="我只负责本平台的售后问题。",
        ),
        user_query="写首诗",
    )
    assert outcome.passed is True


def test_rewrite_changed_required() -> None:
    outcome = check_expectations(
        expected={"rewrite_changed": True},
        values=values(rewritten_query="q"),
        user_query="q",
    )
    assert outcome.passed is False
    assert outcome.attribution == "rewrite"


@pytest.mark.parametrize(
    "expected",
    [
        {"intent": "qa"},
        {"keywords": ["七天"]},
        {"must_cite": True},
        {"min_citations": 1},
    ],
)
def test_individual_expectations_hold(expected: dict[str, object]) -> None:
    outcome = check_expectations(expected=expected, values=values(), user_query="q")
    assert outcome.passed is True
