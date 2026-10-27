"""RAG 纯函数测试：阈值分档、RRF 融合、缓存键（Spec §7.2 / §7.3）。"""

from __future__ import annotations

import pytest

from app.common.config import Settings
from app.rag.cache import (
    ACTIVE_INDEX_KEY,
    TtlPolicy,
    checkpoint_key,
    confirm_token_key,
    fact_cache_key,
    faq_cache_key,
    idem_cache_key,
    local_cache_key,
    lock_key,
    normalize_query,
    query_hash,
    rag_cache_key,
    rate_limit_key,
    session_key,
)
from app.rag.retriever import (
    ANSWER_CAVEAT_NOTE,
    ConfidenceBand,
    classify_confidence,
    classify_with_settings,
    reciprocal_rank_fusion,
)

HIGH, LOW = 0.75, 0.55


@pytest.mark.parametrize(
    ("top1", "expected"),
    [
        (1.0, ConfidenceBand.ANSWER),
        (0.75, ConfidenceBand.ANSWER),
        (0.7499, ConfidenceBand.ANSWER_WITH_CAVEAT),
        (0.55, ConfidenceBand.ANSWER_WITH_CAVEAT),
        (0.5499, ConfidenceBand.FALLBACK),
        (0.0, ConfidenceBand.FALLBACK),
    ],
)
def test_confidence_bands(top1: float, expected: ConfidenceBand) -> None:
    assert classify_confidence(top1, high=HIGH, low=LOW) is expected


def test_confidence_rejects_out_of_range_score() -> None:
    with pytest.raises(ValueError, match="top1"):
        classify_confidence(1.2, high=HIGH, low=LOW)
    with pytest.raises(ValueError, match="top1"):
        classify_confidence(-0.1, high=HIGH, low=LOW)


def test_confidence_rejects_inverted_thresholds() -> None:
    with pytest.raises(ValueError, match="阈值配置非法"):
        classify_confidence(0.6, high=0.5, low=0.6)


def test_classify_with_settings_uses_configured_thresholds() -> None:
    settings = Settings(_env_file=None, rag_top1_threshold=0.9, rag_low_threshold=0.4)
    assert classify_with_settings(0.85, settings) is ConfidenceBand.ANSWER_WITH_CAVEAT
    assert classify_with_settings(0.95, settings) is ConfidenceBand.ANSWER
    assert classify_with_settings(0.2, settings) is ConfidenceBand.FALLBACK


def test_caveat_note_is_non_empty() -> None:
    assert "不完整" in ANSWER_CAVEAT_NOTE


def test_rrf_prefers_doc_present_in_both_channels() -> None:
    fused = reciprocal_rank_fusion(
        ["a", "b", "c"],
        ["c", "d"],
        k=60,
        top_n=3,
    )
    assert [hit.doc_id for hit in fused] == ["c", "a", "b"]
    assert fused[0].vector_rank == 3
    assert fused[0].keyword_rank == 1


def test_rrf_is_deterministic_and_handles_duplicates() -> None:
    first = reciprocal_rank_fusion(["a", "a", "b"], ["b"], top_n=5)
    second = reciprocal_rank_fusion(["a", "a", "b"], ["b"], top_n=5)
    assert [hit.doc_id for hit in first] == [hit.doc_id for hit in second]
    assert len(first) == 2  # 同一通道内重复只计一次


def test_rrf_respects_top_n_and_k() -> None:
    fused = reciprocal_rank_fusion(["a", "b", "c", "d"], [], top_n=2)
    assert [hit.doc_id for hit in fused] == ["a", "b"]
    with pytest.raises(ValueError, match="k"):
        reciprocal_rank_fusion(["a"], [], k=0)
    with pytest.raises(ValueError, match="top_n"):
        reciprocal_rank_fusion(["a"], [], top_n=0)


def test_rrf_score_math() -> None:
    fused = reciprocal_rank_fusion(["a"], ["a"])
    expected = 2 * (1.0 / 61)
    assert fused[0].score == pytest.approx(expected)


def test_query_normalization_and_hash_stability() -> None:
    assert normalize_query("  退款   多久\t到账  ") == "退款 多久 到账"
    assert query_hash("退款  多久到账") == query_hash("退款 多久到账")
    assert query_hash("a") != query_hash("b")
    assert len(query_hash("x")) == 16


def test_cache_keys_follow_spec_patterns() -> None:
    assert rag_cache_key("v3", "退款多久到账") == f"rag:v3:{query_hash('退款多久到账')}"
    assert rag_cache_key("v3", "q") != rag_cache_key("v4", "q")
    assert local_cache_key("v3", "q") == f"lc:{query_hash('q|v3')}"
    assert faq_cache_key("怎么退") == f"faq:{query_hash('怎么退')}"
    assert fact_cache_key("SKU1") == "fact:sku:SKU1"
    assert session_key("s1") == "sess:s1"
    assert checkpoint_key("s1") == "ckpt:s1"
    assert idem_cache_key("AS1:create") == "idem:AS1:create"
    assert lock_key("AS1") == "lock:as:AS1"
    assert confirm_token_key("ct_1") == "confirm:ct_1"
    assert rate_limit_key("u1", "chat") == "rl:u1:chat"
    assert ACTIVE_INDEX_KEY == "idx:active"


def test_ttl_policy_from_settings() -> None:
    settings = Settings(
        _env_file=None,
        rag_cache_ttl_sec=1,
        faq_cache_ttl_sec=2,
        fact_cache_ttl_sec=3,
        session_ttl_hours=1,
        idempotency_ttl_sec=9,
    )
    policy = TtlPolicy.from_settings(settings)
    assert policy == TtlPolicy(
        rag_seconds=1, faq_seconds=2, fact_seconds=3, session_seconds=3600, idem_seconds=9
    )
