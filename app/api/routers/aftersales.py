"""售后办理接口（Spec §9.2 ``/aftersales/*``）。

M0 只固定契约（路径、请求/响应模型、幂等头、错误码），实现随 M2 的
状态机 + 幂等 + outbox 一起落地（FR-AS-01..06）。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter

from app.api.deps import IdempotencyKeyDep, PrincipalDep
from app.api.errors import not_implemented
from app.api.responses import ERROR_RESPONSES, SuccessEnvelope
from app.api.schemas import (
    AfterSalesDetail,
    ApplyData,
    ApplyRequest,
    CancelData,
)

router = APIRouter(prefix="/aftersales", tags=["aftersales"], responses=ERROR_RESPONSES)


@router.post(
    "/apply",
    summary="发起售后（写，幂等 + 双确认）",
    response_model=SuccessEnvelope[ApplyData],
    responses={501: ERROR_RESPONSES[501]},
)
async def apply_after_sales(
    payload: ApplyRequest,
    principal: PrincipalDep,
    idempotency_key: IdempotencyKeyDep,
) -> dict[str, Any]:
    # TODO(M2): confirm_token 校验 → 状态机 DRAFT→PENDING_APPROVAL → INSERT + outbox
    not_implemented("发起售后（状态机 + 幂等 + outbox）", "M2")


@router.get(
    "/{biz_id}",
    summary="查询售后单进度与时间线",
    response_model=SuccessEnvelope[AfterSalesDetail],
)
async def get_after_sales(biz_id: str, principal: PrincipalDep) -> dict[str, Any]:
    # TODO(M2): 读己之写校验 + 时间线聚合
    not_implemented("售后单进度查询（读己之写）", "M2")


@router.post(
    "/{biz_id}/cancel",
    summary="取消售后单（幂等）",
    response_model=SuccessEnvelope[CancelData],
)
async def cancel_after_sales(
    biz_id: str,
    principal: PrincipalDep,
    idempotency_key: IdempotencyKeyDep,
) -> dict[str, Any]:
    # TODO(M2): 状态机 CANCEL 守卫 + 乐观锁
    not_implemented("售后单取消", "M2")
