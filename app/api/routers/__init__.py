"""路由注册表：``create_app`` 只从这里取 router，避免 main.py 变成"路由清单"。"""

from __future__ import annotations

from fastapi import APIRouter

from app.api.routers import (
    admin,
    aftersales,
    approval,
    auth,
    chat,
    orders,
    traces,
)
from app.api.routers import (
    eval as eval_router,
)

#: 挂在 ``settings.api_prefix``（默认 ``/api/v1``）下的路由。
API_ROUTERS: tuple[APIRouter, ...] = (
    auth.router,
    chat.router,
    aftersales.router,
    orders.router,
    approval.router,
    eval_router.router,
    traces.router,
    admin.router,
)

__all__ = ["API_ROUTERS"]
