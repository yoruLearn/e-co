"""评测回归接口（Spec §9.2 ``POST /eval/run``，ops/admin）。"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends

from app.api.deps import require_roles
from app.api.errors import not_implemented
from app.api.responses import ERROR_RESPONSES, SuccessEnvelope
from app.api.schemas import EvalRunData, EvalRunRequest
from app.common.security import Principal, Role

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
) -> dict[str, Any]:
    # TODO(M1): 加载用例 → 执行 → 断言/归因 → 写 eval_run → 返回指标
    not_implemented("评测执行器与门禁报表", "M1")
