"""登录换 JWT（Spec §9.2 ``POST /auth/login``）。

范围说明：Spec 未定义用户源（对接用户中心属 M4）。M0 仅在 ``dev/test`` 下签发令牌用于
本地联调与自动化测试；staging/prod 返回 ``AS-5002``，**不做假登录**。
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter

from app.api.deps import SettingsDep
from app.api.errors import AppError, ErrorCode, not_implemented
from app.api.responses import ERROR_RESPONSES, SuccessEnvelope, ok
from app.api.schemas import LoginRequest, TokenData
from app.common.security import create_access_token

logger = logging.getLogger("app.api.auth")

router = APIRouter(prefix="/auth", tags=["auth"], responses=ERROR_RESPONSES)


@router.post(
    "/login",
    summary="登录换 JWT（dev/test only）",
    response_model=SuccessEnvelope[TokenData],
)
async def login(payload: LoginRequest, settings: SettingsDep) -> dict[str, Any]:
    if not settings.dev_login_allowed:
        not_implemented("真实登录（对接用户中心）", "M4")

    if not payload.user_id.strip():  # pragma: no cover - pydantic 已校验
        raise AppError(ErrorCode.PARAM_INVALID, "user_id 不能为空")

    token, expires_in = create_access_token(
        user_id=payload.user_id,
        role=payload.role,
        settings=settings,
    )
    logger.info(
        "auth.login.dev",
        extra={"user_id": payload.user_id, "role": str(payload.role), "dev_mode": True},
    )
    return ok(
        TokenData(
            access_token=token,
            expires_in=expires_in,
            user_id=payload.user_id,
            role=payload.role,
        ).model_dump()
    )
