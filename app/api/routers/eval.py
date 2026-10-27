"""评测回归接口（Spec §9.2 ``POST /eval/run``，ops/admin）。

同步执行整轮评测并返回指标（90 条 mock 回放秒级；真实模型在 staging 跑，分钟级）。
结果落 ``eval_run`` 表；落库失败只告警，不影响接口返回（评测报告本身仍可用）。
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Any
from uuid import uuid4

from fastapi import APIRouter, Depends, Request

from app.api.deps import require_roles
from app.api.errors import AppError, ErrorCode
from app.api.responses import ERROR_RESPONSES, SuccessEnvelope, ok
from app.api.schemas import EvalRunData, EvalRunRequest
from app.common.logging import get_logger, log_event
from app.common.security import Principal, Role
from app.eval.cases import load_cases
from app.eval.executor import filter_cases, run_suite
from app.eval.graph_executor import GraphCaseExecutor
from app.eval.runner import GateThresholds
from app.eval.store import save_report

logger = get_logger("app.api.eval")

router = APIRouter(prefix="/eval", tags=["eval"], responses=ERROR_RESPONSES)

EvalOperator = Annotated[Principal, Depends(require_roles(Role.OPS, Role.ADMIN))]


@router.post(
    "/run",
    summary="评测回归（四类用例 + 门禁）",
    response_model=SuccessEnvelope[EvalRunData],
)
async def run_eval(
    payload: EvalRunRequest,
    principal: EvalOperator,
    request: Request,
) -> dict[str, Any]:
    settings = request.app.state.settings
    runtime = request.app.state.agent
    cases_dir = Path(settings.eval_cases_dir)
    cases = filter_cases(load_cases(cases_dir), payload.categories)
    if not cases:
        raise AppError(
            ErrorCode.BUSINESS_RULE,
            "没有可执行的评测用例",
            detail={"dir": str(cases_dir), "categories": [str(c) for c in payload.categories]},
        )

    run_id = f"ev_{uuid4().hex[:12]}"
    log_event(
        logger,
        "eval.run.start",
        run_id=run_id,
        suite=payload.suite,
        cases=len(cases),
        operator=principal.user_id,
    )
    report = await run_suite(
        run_id=run_id,
        suite=payload.suite,
        cases=cases,
        executor=GraphCaseExecutor(runtime.graph),
        thresholds=GateThresholds(),
    )
    persisted = await save_report(request.app.state.clients.session_factory, report)
    log_event(
        logger,
        "eval.run.done",
        run_id=run_id,
        passed=report.passed,
        total=report.total,
        gate=report.gate.passed,
        persisted=persisted,
    )

    data = EvalRunData(
        run_id=run_id,
        status="succeeded" if report.gate.passed else "gate_failed",
        total=report.total,
        pass_cnt=report.passed,
        completion_rate=round(report.completion_rate, 4),
        citation_acc=round(report.citation_accuracy, 4),
        p95_ms=report.p95_ms,
        gate_passed=report.gate.passed,
        attribution=report.attribution,
        duration_ms=report.duration_ms,
    )
    return ok(data.model_dump())
