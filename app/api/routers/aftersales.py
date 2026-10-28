"""售后办理接口（Spec §9.2 ``/aftersales/*``）。

- apply：Idempotency-Key 必填 → 只读工具校验订单/权限 → 幂等占位 → 消费 confirm_token
  → 状态机受理（>500 进审批）→ outbox；重放返回首次响应；
- detail：读己之写（buyer 仅本人）；
- cancel：幂等 + 状态机 CANCEL。

状态变化只发生在 ``AfterSalesService`` 内（经 ``transition_biz``）。
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from fastapi import APIRouter, Request

from app.api.deps import IdempotencyKeyDep, PrincipalDep
from app.api.errors import AppError, ErrorCode, IdempotencyConflictError
from app.api.responses import ERROR_RESPONSES, SuccessEnvelope, ok
from app.api.schemas import AfterSalesDetail, ApplyData, ApplyRequest, CancelData
from app.common.trace import get_trace_id
from app.services.aftersales import ApplyCommand
from app.services.idempotency import params_hash
from app.services.idempotency_store import StoreDecision

router = APIRouter(prefix="/aftersales", tags=["aftersales"], responses=ERROR_RESPONSES)


def _require_idem_key(key: str | None) -> str:
    if not key:
        raise AppError(
            ErrorCode.PARAM_INVALID,
            "缺少 Idempotency-Key 请求头",
            detail={"header": "Idempotency-Key"},
        )
    return key


@router.post(
    "/apply",
    summary="发起售后（写，幂等 + 双确认）",
    response_model=SuccessEnvelope[ApplyData],
)
async def apply_after_sales(
    payload: ApplyRequest,
    principal: PrincipalDep,
    idempotency_key: IdempotencyKeyDep,
    request: Request,
) -> dict[str, Any]:
    idem_key = _require_idem_key(idempotency_key)
    tools = request.app.state.order_tools
    service = request.app.state.aftersales
    confirm = request.app.state.confirm
    idem = request.app.state.idem
    trace_id = get_trace_id() or ""

    order_result = await tools.get_order(
        payload.order_id, user_id=principal.user_id, role=principal.role.value
    )
    order_amount = Decimal(str(order_result.data["amount"]))
    amount = Decimal(str(payload.amount)) if payload.amount is not None else order_amount
    if amount > order_amount:
        raise AppError(
            ErrorCode.BUSINESS_RULE,
            "退款金额不能超过订单实付金额",
            detail={"amount": str(amount), "order_amount": str(order_amount)},
        )

    request_payload = {
        "order_id": payload.order_id,
        "action": payload.action.value,
        "reason": payload.reason,
        "amount": str(amount),
    }
    begin = await idem.begin(idem_key, params_hash(request_payload))
    if begin.decision is StoreDecision.CONFLICT:
        raise IdempotencyConflictError(idem_key)
    if begin.decision is StoreDecision.REPLAY and begin.response is not None:
        return ok(begin.response)

    try:
        await confirm.consume(
            payload.confirm_token,
            user_id=principal.user_id,
            action=payload.action.value,
            order_id=payload.order_id,
            amount=amount,
        )
        outcome = await service.apply(
            ApplyCommand(
                user_id=principal.user_id,
                order_id=payload.order_id,
                action=payload.action.value,
                reason_code=payload.reason,
                amount=amount,
                trace_id=trace_id,
                idem_key=idem_key,
            )
        )
    except AppError:
        await idem.release(idem_key)
        raise

    data = ApplyData(
        biz_id=outcome.biz_id,
        status=outcome.status,
        approval_required=outcome.approval_required,
    ).model_dump()
    await idem.complete(idem_key, data)
    return ok(data)


@router.get(
    "/{biz_id}",
    summary="查询售后单进度与时间线",
    response_model=SuccessEnvelope[AfterSalesDetail],
)
async def get_after_sales(biz_id: str, principal: PrincipalDep, request: Request) -> dict[str, Any]:
    service = request.app.state.aftersales
    detail = await service.get(biz_id, user_id=principal.user_id, role=principal.role.value)
    return ok(AfterSalesDetail(**detail).model_dump())


@router.post(
    "/{biz_id}/cancel",
    summary="取消售后单（幂等）",
    response_model=SuccessEnvelope[CancelData],
)
async def cancel_after_sales(
    biz_id: str,
    principal: PrincipalDep,
    idempotency_key: IdempotencyKeyDep,
    request: Request,
) -> dict[str, Any]:
    idem_key = _require_idem_key(idempotency_key)
    service = request.app.state.aftersales
    idem = request.app.state.idem
    trace_id = get_trace_id() or ""

    begin = await idem.begin(idem_key, params_hash({"biz_id": biz_id, "action": "cancel"}))
    if begin.decision is StoreDecision.CONFLICT:
        raise IdempotencyConflictError(idem_key)
    if begin.decision is StoreDecision.REPLAY and begin.response is not None:
        return ok(begin.response)

    try:
        outcome = await service.cancel(
            biz_id,
            user_id=principal.user_id,
            role=principal.role.value,
            trace_id=trace_id,
        )
    except AppError:
        await idem.release(idem_key)
        raise
    data = CancelData(
        biz_id=outcome.biz_id, status=outcome.status, version=outcome.version
    ).model_dump()
    await idem.complete(idem_key, data)
    return ok(data)
