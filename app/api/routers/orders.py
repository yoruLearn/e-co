"""订单只读查询（工作台，Spec §9.2 ``GET /orders/{order_id}``）。

M2：走只读工具（Mock Provider）；buyer 仅限本人订单，agent 可查任意。
真实 Provider 适配在 M4（§21 Q1：staging 联调是上线硬条件）。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request

from app.api.deps import PrincipalDep
from app.api.responses import ERROR_RESPONSES, SuccessEnvelope, ok
from app.api.schemas import OrderData

router = APIRouter(prefix="/orders", tags=["orders"], responses=ERROR_RESPONSES)


@router.get(
    "/{order_id}",
    summary="订单只读查询（工作台）",
    response_model=SuccessEnvelope[OrderData],
)
async def get_order(order_id: str, principal: PrincipalDep, request: Request) -> dict[str, Any]:
    tools = request.app.state.order_tools
    result = await tools.get_order(order_id, user_id=principal.user_id, role=principal.role.value)
    return ok(OrderData(**result.data).model_dump())
