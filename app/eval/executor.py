"""评测执行：跑图 → 断言 → 归因 → 汇总 → 门禁（Spec §13.3 / FR-OBS-02/03）。

M1 的执行器是**同步跑完**的（90 条 × ~2s，用 mock 回放只需秒级；真实模型在 staging 跑）。
HTTP 层用 ``asyncio.to_thread`` 之外的方式不引入——评测本来就是运维动作，可以慢。
"""

from __future__ import annotations

import math
import time
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Protocol

from app.eval.cases import EvalCase, EvalCategory
from app.eval.runner import GateResult, GateThresholds, evaluate_gate


@dataclass(frozen=True, slots=True)
class CaseResult:
    case_key: str
    category: str
    passed: bool
    failures: list[str] = field(default_factory=list)
    attribution: str | None = None
    latency_ms: int = 0
    intent: str = ""
    band: str = ""
    citations: int = 0
    answer: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "case_key": self.case_key,
            "category": self.category,
            "passed": self.passed,
            "failures": self.failures,
            "attribution": self.attribution,
            "latency_ms": self.latency_ms,
            "intent": self.intent,
            "band": self.band,
            "citations": self.citations,
            "answer": self.answer[:200],
        }


@dataclass(frozen=True, slots=True)
class EvalReport:
    run_id: str
    suite: str
    total: int
    passed: int
    completion_rate: float
    citation_accuracy: float
    p95_ms: int
    attribution: dict[str, int]
    gate: GateResult
    duration_ms: int
    cases: list[CaseResult] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "suite": self.suite,
            "total": self.total,
            "passed": self.passed,
            "completion_rate": round(self.completion_rate, 4),
            "citation_accuracy": round(self.citation_accuracy, 4),
            "p95_ms": self.p95_ms,
            "attribution": self.attribution,
            "gate": self.gate.as_dict(),
            "duration_ms": self.duration_ms,
            "failures": [case.as_dict() for case in self.cases if not case.passed],
        }


class CaseExecutor(Protocol):
    async def run(self, case: EvalCase) -> CaseResult: ...


def percentile(values: list[int], ratio: float) -> int:
    """最近秩百分位（P95 用，避免插值带来的"好看"假象）。"""
    if not values:
        return 0
    ordered = sorted(values)
    index = max(0, math.ceil(ratio * len(ordered)) - 1)
    return ordered[min(index, len(ordered) - 1)]


def _citation_accuracy(cases: list[CaseResult], all_cases: list[EvalCase]) -> float:
    """引用正确率 = 要求引用的用例中，确实带引用且断言通过的比例。"""
    requiring = [
        case.case_key
        for case in all_cases
        if case.expectation.get("must_cite") or case.expectation.get("min_citations")
    ]
    if not requiring:
        return 1.0
    by_key = {case.case_key: case for case in cases}
    passed = sum(
        1
        for key in requiring
        if (result := by_key.get(key)) is not None and result.passed and result.citations > 0
    )
    return passed / len(requiring)


def summarize_attribution(cases: list[CaseResult]) -> dict[str, int]:
    counter: Counter[str] = Counter(
        case.attribution for case in cases if not case.passed and case.attribution
    )
    return dict(counter)


async def run_suite(
    *,
    run_id: str,
    suite: str,
    cases: list[EvalCase],
    executor: CaseExecutor,
    thresholds: GateThresholds | None = None,
) -> EvalReport:
    started = time.perf_counter()
    results: list[CaseResult] = []
    for case in cases:
        results.append(await executor.run(case))

    total = len(results)
    passed = sum(1 for result in results if result.passed)
    completion_rate = passed / total if total else 0.0
    citation_accuracy = _citation_accuracy(results, cases)
    p95 = percentile([result.latency_ms for result in results], 0.95)
    gate = evaluate_gate(
        completion_rate=completion_rate,
        citation_accuracy=citation_accuracy,
        p95_regression=0.0,
        thresholds=thresholds,
    )
    return EvalReport(
        run_id=run_id,
        suite=suite,
        total=total,
        passed=passed,
        completion_rate=completion_rate,
        citation_accuracy=citation_accuracy,
        p95_ms=p95,
        attribution=summarize_attribution(results),
        gate=gate,
        duration_ms=int((time.perf_counter() - started) * 1000),
        cases=results,
    )


def filter_cases(cases: list[EvalCase], categories: list[EvalCategory] | None) -> list[EvalCase]:
    if not categories:
        return cases
    allowed = {str(category) for category in categories}
    return [case for case in cases if str(case.category) in allowed]
