"""FastAPI 依赖注入：配置、客户端、鉴权、幂等键、trace。

约定：所有依赖都在 ``create_app`` 设置的 ``app.state`` 上取对象，因此测试里通过
``create_app(settings=...)`` 即可整体替换配置，不需要 monkeypatch 环境变量。
"""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Annotated

from fastapi import Depends, Header, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.api.errors import AppError, ErrorCode
from app.common.clients import AppClients
from app.common.config import Settings
from app.common.security import Principal, Role, decode_access_token
from app.common.trace import get_trace_id
from app.services.health import ReadinessService

IDEMPOTENCY_KEY_PATTERN = re.compile(r"^[A-Za-z0-9_.:-]{8,140}$")

_bearer_scheme = HTTPBearer(
    auto_error=False,
    scheme_name="BearerJWT",
    description="Spec §9.1：Authorization: Bearer <JWT>，payload 含 user_id/role/exp",
)


def get_app_settings(request: Request) -> Settings:
    settings: Settings = request.app.state.settings
    return settings


def get_clients(request: Request) -> AppClients:
    clients: AppClients = request.app.state.clients
    return clients


def get_readiness_service(request: Request) -> ReadinessService:
    service: ReadinessService = request.app.state.readiness
    return service


def get_uptime_seconds(request: Request) -> float:
    started_at: float = request.app.state.started_at
    import time

    return time.time() - started_at


SettingsDep = Annotated[Settings, Depends(get_app_settings)]
ClientsDep = Annotated[AppClients, Depends(get_clients)]
ReadinessDep = Annotated[ReadinessService, Depends(get_readiness_service)]


def get_current_principal(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer_scheme)],
    settings: SettingsDep,
) -> Principal:
    if credentials is None or not credentials.credentials:
        raise AppError(
            ErrorCode.UNAUTHORIZED,
            "缺少 Bearer 令牌",
            detail={"hint": "Authorization: Bearer <JWT>"},
        )
    return decode_access_token(credentials.credentials, settings)


PrincipalDep = Annotated[Principal, Depends(get_current_principal)]


def require_roles(*roles: Role) -> Callable[..., Principal]:
    """角色校验依赖工厂：``Depends(require_roles(Role.OPS, Role.ADMIN))``。"""
    allowed = tuple(roles)

    def _dependency(principal: PrincipalDep) -> Principal:
        if not principal.has_any_role(allowed):
            raise AppError(
                ErrorCode.FORBIDDEN,
                "当前角色无权访问该接口",
                detail={
                    "required_roles": [str(item) for item in allowed],
                    "actual_role": str(principal.role),
                },
            )
        return principal

    return _dependency


def get_trace_context() -> str:
    return get_trace_id() or ""


TraceDep = Annotated[str, Depends(get_trace_context)]


def get_idempotency_key(
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> str | None:
    """解析并校验 ``Idempotency-Key: {bizId}:{action}``（Spec §9.1）。"""
    if idempotency_key is None:
        return None
    candidate = idempotency_key.strip()
    if not IDEMPOTENCY_KEY_PATTERN.match(candidate):
        raise AppError(
            ErrorCode.PARAM_INVALID,
            "Idempotency-Key 格式非法",
            detail={
                "expected": "{bizId}:{action}",
                "pattern": IDEMPOTENCY_KEY_PATTERN.pattern,
            },
        )
    return candidate


IdempotencyKeyDep = Annotated[str | None, Depends(get_idempotency_key)]
