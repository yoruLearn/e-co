"""评测用例加载与发布门禁测试（Spec §13.3）。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.eval.cases import (
    CaseLoadError,
    EvalCase,
    EvalCategory,
    count_by_category,
    load_cases,
)
from app.eval.runner import GateThresholds, evaluate_gate

REPO_ROOT = Path(__file__).resolve().parents[2]
CASES_DIR = REPO_ROOT / "evals" / "cases"


def _write(tmp_path: Path, name: str, payload: object) -> Path:
    path = tmp_path / name
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


def test_load_repository_sample_cases() -> None:
    cases = load_cases(CASES_DIR)
    assert len(cases) >= 4
    categories = {case.category for case in cases}
    assert categories == set(EvalCategory)


def test_count_by_category_covers_all_four() -> None:
    counts = count_by_category(load_cases(CASES_DIR))
    assert set(counts) == {category.value for category in EvalCategory}
    assert all(value >= 1 for value in counts.values())


def test_load_list_and_wrapped_formats(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "a.json",
        [
            {
                "case_key": "c1",
                "category": "typical",
                "input": {"query": "退款多久到账"},
                "expectation": {"intent": "qa"},
            }
        ],
    )
    _write(
        tmp_path,
        "b.json",
        {
            "cases": [
                {
                    "case_key": "c2",
                    "category": "no_answer",
                    "input": {"query": "今天天气"},
                    "expectation": {"must_refuse": True},
                }
            ]
        },
    )
    cases = load_cases(tmp_path)
    assert [case.case_key for case in cases] == ["c1", "c2"]


def test_duplicate_case_key_is_rejected(tmp_path: Path) -> None:
    payload = [{"case_key": "dup", "category": "typical", "input": {}, "expectation": {}}]
    _write(tmp_path, "a.json", payload)
    _write(tmp_path, "b.json", payload)
    with pytest.raises(CaseLoadError, match="重复"):
        load_cases(tmp_path)


def test_invalid_json_is_reported_with_filename(tmp_path: Path) -> None:
    (tmp_path / "broken.json").write_text("{not json", encoding="utf-8")
    with pytest.raises(CaseLoadError, match=r"broken\.json"):
        load_cases(tmp_path)


def test_invalid_category_is_rejected(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "a.json",
        [{"case_key": "x", "category": "unknown", "input": {}, "expectation": {}}],
    )
    with pytest.raises(CaseLoadError, match="用例校验失败"):
        load_cases(tmp_path)


def test_extra_fields_are_rejected(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "a.json",
        [
            {
                "case_key": "x",
                "category": "typical",
                "input": {},
                "expectation": {},
                "surprise": 1,
            }
        ],
    )
    with pytest.raises(CaseLoadError):
        load_cases(tmp_path)


def test_missing_directory_returns_empty(tmp_path: Path) -> None:
    assert load_cases(tmp_path / "nope") == []


def test_eval_case_model_rejects_empty_key() -> None:
    with pytest.raises(ValueError):
        EvalCase(case_key="", category=EvalCategory.TYPICAL)


def test_gate_passes_when_all_metrics_ok() -> None:
    result = evaluate_gate(completion_rate=0.95, citation_accuracy=0.99, p95_regression=0.05)
    assert result.passed is True
    assert result.failures == []
    assert result.as_dict()["passed"] is True


@pytest.mark.parametrize(
    ("kwargs", "needle"),
    [
        ({"completion_rate": 0.5, "citation_accuracy": 0.99}, "完成率"),
        ({"completion_rate": 0.99, "citation_accuracy": 0.5}, "引用正确率"),
        (
            {"completion_rate": 0.99, "citation_accuracy": 0.99, "p95_regression": 0.5},
            "P95",
        ),
        (
            {"completion_rate": 0.99, "citation_accuracy": 0.99, "illegal_transitions": 1},
            "非法状态迁移",
        ),
    ],
)
def test_gate_fails_on_each_dimension(kwargs: dict[str, float], needle: str) -> None:
    result = evaluate_gate(**kwargs)  # type: ignore[arg-type]
    assert result.passed is False
    assert any(needle in failure for failure in result.failures)


def test_gate_thresholds_are_configurable() -> None:
    strict = GateThresholds(completion_rate=0.99, citation_accuracy=0.99)
    assert (
        evaluate_gate(completion_rate=0.95, citation_accuracy=0.99, thresholds=strict).passed
        is False
    )
    assert evaluate_gate(completion_rate=0.95, citation_accuracy=0.99).passed is True
