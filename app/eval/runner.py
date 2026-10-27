"""评测执行与门禁（Spec §13.3）。

M0 只实现**门禁判定**（纯函数）：完成率 ≥ 90%、引用正确率 ≥ 95%、P95 不劣化 > 10%、
非法迁移 = 0。评测执行器（断言、归因、报表）在 M1 随 ``/eval/run`` 一起落地。
"""

from __future__ import annotations

from dataclasses import dataclass

from app.eval.cases import EvalCase


@dataclass(frozen=True, slots=True)
class GateThresholds:
    completion_rate: float = 0.90
    citation_accuracy: float = 0.95
    max_p95_regression: float = 0.10
    max_illegal_transitions: int = 0


@dataclass(frozen=True, slots=True)
class GateResult:
    passed: bool
    failures: list[str]
    metrics: dict[str, float]

    def as_dict(self) -> dict[str, object]:
        return {"passed": self.passed, "failures": self.failures, "metrics": self.metrics}


def evaluate_gate(
    *,
    completion_rate: float,
    citation_accuracy: float,
    p95_regression: float = 0.0,
    illegal_transitions: int = 0,
    thresholds: GateThresholds | None = None,
) -> GateResult:
    """发布门禁；任一不达标即 ``passed=False``（CI 阻断发布）。"""
    policy = thresholds or GateThresholds()
    failures: list[str] = []
    if completion_rate < policy.completion_rate:
        failures.append(f"完成率 {completion_rate:.2%} < 门禁 {policy.completion_rate:.0%}")
    if citation_accuracy < policy.citation_accuracy:
        failures.append(f"引用正确率 {citation_accuracy:.2%} < 门禁 {policy.citation_accuracy:.0%}")
    if p95_regression > policy.max_p95_regression:
        failures.append(f"P95 劣化 {p95_regression:.1%} > 门禁 {policy.max_p95_regression:.0%}")
    if illegal_transitions > policy.max_illegal_transitions:
        failures.append(f"非法状态迁移 {illegal_transitions} 次（必须为 0）")
    return GateResult(
        passed=not failures,
        failures=failures,
        metrics={
            "completion_rate": completion_rate,
            "citation_accuracy": citation_accuracy,
            "p95_regression": p95_regression,
            "illegal_transitions": float(illegal_transitions),
        },
    )


def run_suite(*, suite: str, cases: list[EvalCase]) -> dict[str, object]:
    """执行评测集并返回完成率/引用正确率/P95/归因分布。

    TODO(M1): 与 ``/eval/run`` 一起实现（断言执行器 + 失败归因 + ``eval_run`` 落库）。
    """
    raise NotImplementedError("评测执行器将在 M1 实现（Spec §13.3）")
