"""评测用例加载（Spec §13.3 四类用例，进 git 的 JSON 集）。

文件格式（``evals/cases/*.json``）::

    [{"case_key": "typical-001",
      "category": "typical",
      "input": {"query": "退款多久到账", "history": []},
      "expectation": {"intent": "qa", "must_cite": true, "keywords": ["3-5 个工作日"]}}]

M0 只做**加载与校验**（用例集可以先进库、进 CI），断言执行在 M1 与 ``/eval/run`` 一起接。
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from enum import StrEnum
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field, ValidationError


class EvalCategory(StrEnum):
    TYPICAL = "typical"
    MULTI_HOP = "multi_hop"
    NO_ANSWER = "no_answer"
    MULTI_TURN = "multi_turn"


class EvalCase(BaseModel):
    """一条评测用例。"""

    model_config = {"extra": "forbid"}

    case_key: str = Field(min_length=1, max_length=64)
    category: EvalCategory
    input: dict[str, Any] = Field(default_factory=dict)
    expectation: dict[str, Any] = Field(default_factory=dict)
    enabled: bool = True


class CaseLoadError(ValueError):
    """用例集格式错误（含文件与条目定位）。"""


def _iter_files(directory: Path) -> Iterable[Path]:
    return sorted(path for path in directory.glob("*.json") if path.is_file())


def _raw_cases(payload: Any) -> list[Any]:
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict) and isinstance(payload.get("cases"), list):
        cases: list[Any] = payload["cases"]
        return cases
    msg = '用例文件必须是 JSON 数组或 {"cases": [...]}'
    raise CaseLoadError(msg)


def load_cases(directory: Path) -> list[EvalCase]:
    """加载目录下全部用例；``case_key`` 全局唯一，重复即失败（防静默覆盖）。"""
    if not directory.exists():
        return []

    loaded: list[EvalCase] = []
    seen: set[str] = set()
    for path in _iter_files(directory):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise CaseLoadError(f"{path.name}: JSON 解析失败 - {exc}") from exc
        for index, raw in enumerate(_raw_cases(payload)):
            try:
                case = EvalCase.model_validate(raw)
            except ValidationError as exc:
                raise CaseLoadError(f"{path.name}[{index}]: 用例校验失败 - {exc}") from exc
            if case.case_key in seen:
                raise CaseLoadError(f"{path.name}[{index}]: case_key 重复 {case.case_key}")
            seen.add(case.case_key)
            loaded.append(case)
    return loaded


def count_by_category(cases: Iterable[EvalCase]) -> dict[str, int]:
    counts = {category.value: 0 for category in EvalCategory}
    for case in cases:
        counts[case.category.value] += 1
    return counts
