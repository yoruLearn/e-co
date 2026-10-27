"""JWT 鉴权与角色模型（Spec §9.1 / §14）。

- 生产：RS256，公钥文件（``JWT_PUBLIC_KEY_PATH``），私钥仅在签发侧（内网/用户中心）；
- dev/test：HS256，便于本地联调（``JWT_HS256_SECRET``）；
- 角色：``buyer / agent / approver / ops / admin``（Spec §1.4）。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any

import jwt

from app.api.errors import AppError, ErrorCode
from app.common.config import Settings

logger = logging.getLogger("app.security")


class Role(StrEnum):
    BUYER = "buyer"
    AGENT = "agent"
    APPROVER = "approver"
    OPS = "ops"
    ADMIN = "admin"


@dataclass(frozen=True, slots=True)
class Principal:
    """已认证主体。``user_id`` 来自 JWT ``sub``。"""

    user_id: str
    role: Role

    def has_any_role(self, roles: tuple[Role, ...]) -> bool:
        return self.role in roles


def _signing_key(settings: Settings, *, for_encode: bool) -> str:
    if settings.jwt_algorithm == "RS256":
        if for_encode:
            private = settings.jwt_private_key()
            if not private:
                raise AppError(ErrorCode.INTERNAL, "服务端缺少 JWT 私钥，无法签发令牌")
            return private
        public = settings.jwt_public_key()
        if not public:
            raise AppError(ErrorCode.INTERNAL, "服务端缺少 JWT 公钥，无法校验令牌")
        return public
    return settings.jwt_hs256_secret.get_secret_value()


def create_access_token(
    *,
    user_id: str,
    role: Role,
    settings: Settings,
    ttl_seconds: int | None = None,
) -> tuple[str, int]:
    """签发 access token，返回 ``(token, expires_in_seconds)``。"""
    ttl = ttl_seconds or settings.jwt_ttl_seconds
    now = datetime.now(tz=UTC)
    payload: dict[str, Any] = {
        "sub": user_id,
        "role": str(role),
        "iss": settings.jwt_issuer,
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(seconds=ttl)).timestamp()),
    }
    token = jwt.encode(payload, _signing_key(settings, for_encode=True), settings.jwt_algorithm)
    return token, ttl


def decode_access_token(token: str, settings: Settings) -> Principal:
    """校验签名/过期/签发者，返回 Principal；任何失败抛 AS-4010。"""
    try:
        claims = jwt.decode(
            token,
            _signing_key(settings, for_encode=False),
            algorithms=[settings.jwt_algorithm],
            issuer=settings.jwt_issuer,
            options={"require": ["exp", "sub", "iss"]},
        )
    except jwt.ExpiredSignatureError as exc:
        raise AppError(ErrorCode.UNAUTHORIZED, "令牌已过期，请重新登录") from exc
    except jwt.InvalidIssuerError as exc:
        raise AppError(ErrorCode.UNAUTHORIZED, "令牌签发者不合法") from exc
    except jwt.InvalidTokenError as exc:
        logger.warning("jwt.invalid", extra={"reason": type(exc).__name__})
        raise AppError(ErrorCode.UNAUTHORIZED, "令牌无效") from exc

    user_id = claims.get("sub")
    role_raw = claims.get("role")
    if not isinstance(user_id, str) or not user_id:
        raise AppError(ErrorCode.UNAUTHORIZED, "令牌缺少 sub")
    if not isinstance(role_raw, str):
        raise AppError(ErrorCode.UNAUTHORIZED, "令牌缺少 role")
    try:
        role = Role(role_raw)
    except ValueError as exc:
        raise AppError(ErrorCode.UNAUTHORIZED, f"未知角色 {role_raw!r}") from exc
    return Principal(user_id=user_id, role=role)
