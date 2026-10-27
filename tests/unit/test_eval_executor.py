"""评测执行器测试：P95、完成率、引用正确率、归因汇总、门禁（FakeExecutor）。"""

from __future__ import annotations

import pytest

from app.eval.cases import EvalCase, EvalCategory
from app.eval.executor import (
    CaseResult,
    EvalReport,
    filter_cases,
    percentile,
    run_suite,
    summarize_attribution,
)


def make_case(
    key: str, category: EvalCategory = EvalCategory.TYPICAL, **expectation: object
) -> EvalCase:
    return EvalCase(
        case_key=key,
        category=category,
        input={"query": f"问题-{key}", "history": []},
        expectation=dict(expectation),
    )


class FakeExecutor:
    def __init__(self, results: dict[str, CaseResult]) -> None:
        self._results = results
        self.seen: list[str] = []

    async def run(self, case: EvalCase) -> CaseResult:
        self.seen.append(case.case_key)
        return self._results[case.case_key]


def result(
    key: str,
    *,
    passed: bool = True,
    latency: int = 100,
    citations: int = 1,
    attribution: str | None = None,
) -> CaseResult:
    return CaseResult(
        case_key=key,
        category="typical",
        passed=passed,
        failures=[] if passed else ["boom"],
        attribution=attribution,
        latency_ms=latency,
        intent="qa",
        band="answer",
        citations=citations,
        answer="答案[[1]]",
    )


def test_percentile_uses_nearest_rank() -> None:
    values = list(range(1, 101))
    assert percentile(values, 0.95) == 95
    assert percentile([], 0.95) == 0
    assert percentile([42], 0.95) == 42


async def test_run_suite_computes_metrics_and_gate() -> None:
    cases = [
        make_case("c1", must_cite=True),
        make_case("c2", must_cite=True),
        make_case("c3"),
        make_case("c4"),
    ]
    executor = FakeExecutor(
        {
            "c1": result("c1", latency=100),
            "c2": result("c2", latency=200),
            "c3": result("c3", latency=300),
            "c4": result("c4", passed=False, latency=400, attribution="retrieve", citations=0),
        }
    )
    report = await run_suite(run_id="ev_test", suite="unit", cases=cases, executor=executor)
    assert isinstance(report, EvalReport)
    assert report.total == 4
    assert report.passed == 3
    assert report.completion_rate == 0.75
    assert report.citation_accuracy == 1.0  # 两条要求引用的都过且有引用
    assert report.p95_ms == 400
    assert report.attribution == {"retrieve": 1}
    assert report.gate.passed is False
    assert executor.seen == ["c1", "c2", "c3", "c4"]


async def test_citation_accuracy_penalises_missing_citations() -> None:
    cases = [make_case("c1", must_cite=True), make_case("c2", must_cite=True)]
    executor = FakeExecutor(
        {
            "c1": result("c1", citations=1),
            "c2": result("c2", citations=0),
        }
    )
    report = await run_suite(run_id="ev", suite="unit", cases=cases, executor=executor)
    assert report.citation_accuracy == 0.5


async def test_run_suite_empty_cases_fails_gate() -> None:
    report = await run_suite(run_id="ev", suite="unit", cases=[], executor=FakeExecutor({}))
    assert report.total == 0
    assert report.completion_rate == 0.0
    assert report.gate.passed is False


def test_summarize_attribution_counts_only_failures() -> None:
    results = [
        result("a", passed=False, attribution="router"),
        result("b", passed=False, attribution="router"),
        result("c", passed=False, attribution=None),
        result("d", passed=True),
    ]
    assert summarize_attribution(results) == {"router": 2}


def test_filter_cases_by_category() -> None:
    cases = [
        make_case("t1", EvalCategory.TYPICAL),
        make_case("n1", EvalCategory.NO_ANSWER),
        make_case("m1", EvalCategory.MULTI_TURN),
    ]
    filtered = filter_cases(cases, [EvalCategory.NO_ANSWER, EvalCategory.MULTI_TURN])
    assert [case.case_key for case in filtered] == ["n1", "m1"]
    assert filter_cases(cases, None) == cases
    assert filter_cases(cases, []) == cases


def test_report_as_dict_contains_failures() -> None:
    report = EvalReport(
        run_id="ev",
        suite="unit",
        total=1,
        passed=0,
        completion_rate=0.0,
        citation_accuracy=0.0,
        p95_ms=10,
        attribution={"router": 1},
        gate=__import__("app.eval.runner", fromlist=["evaluate_gate"]).evaluate_gate(
            completion_rate=0.0, citation_accuracy=0.0
        ),
        duration_ms=10,
        cases=[result("x", passed=False, attribution="router")],
    )
    payload = report.as_dict()
    assert payload["failures"][0]["case_key"] == "x"
    assert payload["attribution"] == {"router": 1}


@pytest.mark.parametrize("ratio", [0.5, 0.9, 0.95, 0.99])
def test_percentile_monotonic(ratio: float) -> None:
    values = [10, 20, 30, 40, 50]
    assert 10 <= percentile(values, ratio) <= 50
