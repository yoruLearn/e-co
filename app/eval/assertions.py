"""评测断言与失败归因（Spec §13.3）。

断言维度按用例类别组合，全部基于图跑完后的 ``state``：

| 维度 | 判据 | 失败归因 |
| --- | --- | --- |
| ``intent`` | 与期望一致 | router |
| ``keywords`` | 关键词出现在答案或引用里 | answer |
| ``must_cite`` | 答案含 ``[[n]]`` 且引用数 ≥ 阈值 | retrieve（无检索）/answer（有检索无引用） |
| ``must_fallback`` / ``must_refuse`` | 走兜底/拒绝且**没有**引用 | retrieve/answer |
| ``rewrite_changed`` | 改写后的 query 与原句不同 | rewrite |

归因不是猜的：按"哪一段的输出不符合预期"定位到具体节点，直接用于评测报告的失败分布。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.agent.citations import has_citation

REFUSAL_MARKERS = ("只负责", "无法", "不处理", "超出范围", "不提供", "不能回答", "转人工")
FALLBACK_MARKERS = ("没有找到", "不够准确", "转人工", "资料中没有")


@dataclass(frozen=True, slots=True)
class AssertionOutcome:
    passed: bool
    failures: list[str]
    attribution: str | None

    def as_dict(self) -> dict[str, object]:
        return {
            "passed": self.passed,
            "failures": self.failures,
            "attribution": self.attribution,
        }


def _answer_text(values: dict[str, Any]) -> str:
    return str(values.get("answer_stream", ""))


def _citation_blob(values: dict[str, Any]) -> str:
    citations = values.get("citations") or []
    parts: list[str] = []
    for item in citations:
        if isinstance(item, dict):
            parts.append(str(item.get("snippet", "")))
            parts.append(str(item.get("section", "")))
        else:  # pydantic 模型（图内/落库后两种形态）
            parts.append(str(getattr(item, "snippet", "")))
            parts.append(str(getattr(item, "section", "")))
    return " ".join(parts)


def _citation_count(values: dict[str, Any]) -> int:
    citations = values.get("citations") or []
    return len(citations) if isinstance(citations, list) else 0


def check_expectations(
    *,
    expected: dict[str, Any],
    values: dict[str, Any],
    user_query: str,
) -> AssertionOutcome:
    failures: list[str] = []
    attribution: str | None = None
    answer = _answer_text(values)
    citations = _citation_count(values)
    band = str(values.get("band", ""))

    expected_intent = expected.get("intent")
    allowed_intents = expected.get("intent_any")
    actual_intent = str(values.get("intent", ""))
    if isinstance(allowed_intents, list) and allowed_intents:
        if actual_intent not in [str(item) for item in allowed_intents]:
            failures.append(f"意图不符：期望 {allowed_intents} 之一，实际 {actual_intent}")
            attribution = attribution or "router"
    elif expected_intent and actual_intent != expected_intent:
        failures.append(f"意图不符：期望 {expected_intent}，实际 {actual_intent}")
        attribution = attribution or "router"

    keywords = expected.get("keywords") or []
    haystack = answer + " " + _citation_blob(values)
    missing = [word for word in keywords if str(word) not in haystack]
    if missing:
        failures.append(f"缺少关键词：{missing}")
        attribution = attribution or "answer"

    if expected.get("must_cite") and not has_citation(answer):
        failures.append("答案未携带 [[n]] 引用")
        attribution = attribution or ("answer" if citations else "retrieve")

    min_citations = int(expected.get("min_citations", 0) or 0)
    if citations < min_citations:
        failures.append(f"引用数不足：期望 ≥{min_citations}，实际 {citations}")
        attribution = attribution or "retrieve"

    if expected.get("must_fallback"):
        looks_fallback = band == "fallback" or any(marker in answer for marker in FALLBACK_MARKERS)
        if not looks_fallback:
            failures.append("低置信场景未走兜底")
            attribution = attribution or "answer"
        if expected.get("must_cite") is not True and citations and band != "fallback":
            failures.append("兜底场景不应返回引用")
            attribution = attribution or "retrieve"

    if expected.get("must_refuse"):
        refused = any(marker in answer for marker in REFUSAL_MARKERS) or band == "fallback"
        if not refused:
            failures.append("越界问题未拒绝")
            attribution = attribution or "answer"

    if expected.get("rewrite_changed"):
        rewritten = str(values.get("rewritten_query") or "")
        if not rewritten or rewritten == user_query:
            failures.append("多轮指代未改写")
            attribution = attribution or "rewrite"

    if expected.get("must_not_fabricate") and citations == 0 and expected.get("must_cite"):
        failures.append("无检索依据却给出了知识性回答")
        attribution = attribution or "retrieve"

    return AssertionOutcome(passed=not failures, failures=failures, attribution=attribution)
