"""订单只读查询（工作台，Spec §9.2 ``GET /orders/{order_id}``）。

M0 只挂契约；真实 Provider 适配与越权校验在 M2/M4（§21 Q1：staging 联调是上线硬条件）。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter

from app.api.deps import PrincipalDep
from app.api.errors import not_implemented
from app.api.responses import ERROR_RESPONSES, SuccessEnvelope
from app.api.schemas import OrderData

router = APIRouter(prefix="/orders", tags=["orders"], responses=ERROR_RESPONSES)


@router.get(
    "/{order_id}",
    summary="订单只读查询（工作台）",
    response_model=SuccessEnvelope[OrderData],
)
async def get_order(order_id: str, principal: PrincipalDep) -> dict[str, Any]:
    # TODO(M2): Provider 适配 + buyer 仅限本人订单（§8.2 分级授权）
    not_implemented("订单查询（Provider 适配）", "M2")
