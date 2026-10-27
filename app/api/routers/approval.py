"""审批回调（Spec §9.2 ``POST /approval/callback``，HMAC 验签 + 幂等）。

M0 只挂契约。M2 实现要点：

- 验签 ``X-Signature = HMAC-SHA256(secret, body)``，配合内网白名单；
- 回调幂等：``approval_task.callback_count`` + 首次结果复用，重复回调返回首次结果（FR-HITL-03）；
- 状态推进走唯一入口 ``state_machine``（乐观锁 ``version``）。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter

from app.api.errors import not_implemented
from app.api.responses import ERROR_RESPONSES, SuccessEnvelope
from app.api.schemas import ApprovalCallbackData, ApprovalCallbackRequest

router = APIRouter(prefix="/approval", tags=["approval"], responses=ERROR_RESPONSES)


@router.post(
    "/callback",
    summary="审批回调（HMAC 验签，幂等）",
    response_model=SuccessEnvelope[ApprovalCallbackData],
)
async def approval_callback(payload: ApprovalCallbackRequest) -> dict[str, Any]:
    # TODO(M2): 验签 → 幂等 → 状态迁移 APPROVE/REJECT → outbox 事件
    not_implemented("审批回调（验签 + 幂等 + 状态迁移）", "M2")
