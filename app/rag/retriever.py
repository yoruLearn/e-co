"""检索判定与融合（Spec §7.2）。

M0 落地两个纯函数，M1 直接复用：

- ``classify_confidence``：Top1 分数 → 三档行为（作答 / 带提示作答 / 低置信兜底）。
  **Top1 < low 时禁止生成答案**（§7.2 第 4 条 / G2 无引用不直出）。
- ``reciprocal_rank_fusion``：向量 Top20 + 关键词 Top20 → RRF 融合 Top10。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum

from app.common.config import Settings


class ConfidenceBand(StrEnum):
    ANSWER = "answer"
    ANSWER_WITH_CAVEAT = "answer_with_caveat"
    FALLBACK = "fallback"


ANSWER_CAVEAT_NOTE = "以上信息可能不完整，如需准确结论建议转人工核实。"


def classify_confidence(top1: float, *, high: float, low: float) -> ConfidenceBand:
    """Top1 分数三档判定。

    - ``top1 >= high``：直接引用作答；
    - ``low <= top1 < high``：作答并附「可能不完整」提示；
    - ``top1 < low``：低置信兜底，禁止生成。
    """
    if not 0.0 <= top1 <= 1.0:
        msg = f"top1 分数必须在 [0, 1] 内，当前 {top1}"
        raise ValueError(msg)
    if low >= high:
        msg = f"阈值配置非法：low({low}) 必须小于 high({high})"
        raise ValueError(msg)
    if top1 >= high:
        return ConfidenceBand.ANSWER
    if top1 >= low:
        return ConfidenceBand.ANSWER_WITH_CAVEAT
    return ConfidenceBand.FALLBACK


def classify_with_settings(top1: float, settings: Settings) -> ConfidenceBand:
    return classify_confidence(
        top1,
        high=settings.rag_top1_threshold,
        low=settings.rag_low_threshold,
    )


@dataclass(frozen=True, slots=True)
class FusedHit:
    doc_id: str
    score: float
    vector_rank: int | None
    keyword_rank: int | None


def reciprocal_rank_fusion(
    vector_ids: Sequence[str],
    keyword_ids: Sequence[str],
    *,
    k: int = 60,
    top_n: int = 10,
) -> list[FusedHit]:
    """RRF 融合：``score = Σ 1/(k + rank)``，rank 从 1 开始。

    返回按分数降序、同分按首次出现顺序的 Top-N，供 Reranker 二次重排。
    """
    if k <= 0:
        msg = "RRF 参数 k 必须为正整数"
        raise ValueError(msg)
    if top_n <= 0:
        msg = "top_n 必须为正整数"
        raise ValueError(msg)

    scores: dict[str, float] = {}
    vector_rank: dict[str, int] = {}
    keyword_rank: dict[str, int] = {}
    first_seen: dict[str, int] = {}
    counter = 0

    for ids, rank_map in ((vector_ids, vector_rank), (keyword_ids, keyword_rank)):
        for position, doc_id in enumerate(ids, start=1):
            if doc_id in rank_map:  # 同一通道内重复只计首次
                continue
            rank_map[doc_id] = position
            scores[doc_id] = scores.get(doc_id, 0.0) + 1.0 / (k + position)
            if doc_id not in first_seen:
                first_seen[doc_id] = counter
                counter += 1

    ordered = sorted(
        scores.items(),
        key=lambda item: (-item[1], first_seen[item[0]]),
    )[:top_n]
    return [
        FusedHit(
            doc_id=doc_id,
            score=score,
            vector_rank=vector_rank.get(doc_id),
            keyword_rank=keyword_rank.get(doc_id),
        )
        for doc_id, score in ordered
    ]
