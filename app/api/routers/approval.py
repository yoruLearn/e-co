"""审批回调（Spec §9.2 ``POST /approval/callback``，HMAC 验签 + 幂等）。

验签覆盖**原始 body**（不是重新序列化的 JSON），避免键序/空格差异导致验签漂移。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request

from app.api.errors import AppError, ErrorCode
from app.api.responses import ERROR_RESPONSES, SuccessEnvelope, ok
from app.api.schemas import ApprovalCallbackData, ApprovalCallbackRequest
from app.common.trace import get_trace_id
from app.services.approval import SIGNATURE_HEADER, verify_signature

router = APIRouter(prefix="/approval", tags=["approval"], responses=ERROR_RESPONSES)


@router.post(
    "/callback",
    summary="审批回调（HMAC 验签，幂等）",
    response_model=SuccessEnvelope[ApprovalCallbackData],
)
async def approval_callback(request: Request) -> dict[str, Any]:
    body = await request.body()
    settings = request.app.state.settings
    secret = settings.approval_callback_secret.get_secret_value()
    signature = request.headers.get(SIGNATURE_HEADER)
    if not verify_signature(secret, body, signature):
        raise AppError(
            ErrorCode.UNAUTHORIZED,
            "审批回调签名校验失败",
            detail={"header": SIGNATURE_HEADER},
        )
    try:
        payload = ApprovalCallbackRequest.model_validate_json(body)
    except ValueError as exc:
        raise AppError(ErrorCode.PARAM_INVALID, "回调体不合法") from exc

    service = request.app.state.approval
    result = await service.callback(
        task_id=payload.approval_task_id,
        decision=payload.decision,
        approver=payload.approver,
        comment=payload.comment,
        trace_id=get_trace_id() or "",
    )
    return ok(ApprovalCallbackData(**result).model_dump())
