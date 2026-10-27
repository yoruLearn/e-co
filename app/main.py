"""FastAPI 应用装配（Spec §2.1 应用服务层）。

分层顺序（外 → 内）：TraceMiddleware → MetricsMiddleware → CORS → Router。

**启动不连库**：MySQL/Redis/Chroma 客户端在 ``create_app`` 中构造但不建立连接，
依赖健康度由 ``/readyz`` 判定（ADR-0003）。这样 compose 的启动顺序、k8s 的
CrashLoopBackOff、迁移未跑等场景都不会把进程拖死。
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any, Final

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import ORJSONResponse
from prometheus_client import make_asgi_app

from app.agent.runtime import build_agent_runtime, init_agent_runtime
from app.api.handlers import register_exception_handlers
from app.api.middleware import MetricsMiddleware, TraceMiddleware
from app.api.routers import API_ROUTERS
from app.api.routers import health as health_router
from app.common.clients import AppClients, build_clients
from app.common.config import Settings, get_settings
from app.common.logging import configure_logging, update_log_context
from app.common.metrics import APP_INFO
from app.services.health import ReadinessService, build_readiness_service

logger = logging.getLogger("app.main")

API_DESCRIPTION: Final[str] = """
电商售后管家对话式售后 Agent。

- **问答链路**：`POST /api/v1/chat/stream`（SSE，引用作答）
- **办事链路**：`POST /api/v1/aftersales/apply`（幂等 + 双确认 + 审批）
- **审批回调**：`POST /api/v1/approval/callback`（HMAC 验签）
- **运维**：`GET /healthz`、`GET /readyz`、`/metrics`

M0 骨架：未实现的业务端点返回 `AS-5002`（501），契约已按 Spec §9.2 固定。
""".strip()

OPENAPI_TAGS: Final[list[dict[str, str]]] = [
    {"name": "ops", "description": "健康探针"},
    {"name": "auth", "description": "鉴权"},
    {"name": "chat", "description": "对话（SSE）"},
    {"name": "aftersales", "description": "售后办理"},
    {"name": "orders", "description": "订单查询"},
    {"name": "approval", "description": "HITL 审批"},
    {"name": "eval", "description": "评测回归"},
    {"name": "traces", "description": "链路追踪"},
    {"name": "admin", "description": "管理后台（审计）"},
]


def _build_lifespan(settings: Settings) -> Any:
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        clients: AppClients = app.state.clients
        update_log_context(
            env=settings.app_env,
            release=settings.release_channel,
            version=settings.app_version,
        )
        APP_INFO.labels(
            version=settings.app_version,
            env=settings.app_env,
            release_channel=settings.release_channel,
        ).set(1)
        logger.info(
            "app.startup",
            extra={
                "env": settings.app_env,
                "api_prefix": settings.api_prefix,
                "mq_enabled": settings.mq_enabled,
                "docs_enabled": settings.docs_enabled,
            },
        )
        # Redis checkpointer 初始化失败会降级为内存（不影响启动，见 init_agent_runtime）
        await init_agent_runtime(app.state.agent)
        try:
            yield
        finally:
            # SSE 长连接先 drain 再关连接（Spec §17.3 滚动替换）
            if settings.shutdown_drain_seconds > 0:
                logger.info(
                    "app.drain",
                    extra={"seconds": settings.shutdown_drain_seconds},
                )
                await asyncio.sleep(settings.shutdown_drain_seconds)
            await app.state.agent.close()
            await clients.aclose()
            logger.info("app.shutdown")

    return lifespan


def create_app(settings: Settings | None = None) -> FastAPI:
    """应用工厂。测试通过传入自定义 ``Settings`` 隔离环境变量。"""
    settings = settings or get_settings()
    configure_logging(settings.log_level, settings.log_format)

    clients = build_clients(settings)
    readiness: ReadinessService = build_readiness_service(settings, clients)

    app = FastAPI(
        title="电商售后管家 API",
        description=API_DESCRIPTION,
        version=settings.app_version,
        openapi_tags=OPENAPI_TAGS,
        lifespan=_build_lifespan(settings),
        default_response_class=ORJSONResponse,
        docs_url="/docs" if settings.docs_enabled else None,
        redoc_url=None,
        openapi_url="/openapi.json" if settings.docs_enabled else None,
        swagger_ui_parameters={"persistAuthorization": True, "displayRequestDuration": True},
    )

    app.state.settings = settings
    app.state.clients = clients
    app.state.readiness = readiness
    app.state.agent = build_agent_runtime(settings, clients)
    app.state.started_at = time.time()

    register_exception_handlers(app)

    if settings.cors_allow_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=settings.cors_allow_origins,
            allow_credentials=True,
            allow_methods=["GET", "POST", "OPTIONS"],
            allow_headers=[
                "Authorization",
                "Content-Type",
                "X-Trace-Id",
                "X-Session-Id",
                "Idempotency-Key",
            ],
            expose_headers=["X-Trace-Id"],
        )
    # 后添加者在更外层：Trace 必须最外层，保证 Metrics 与业务日志都带 trace_id
    app.add_middleware(MetricsMiddleware)
    app.add_middleware(TraceMiddleware)

    app.mount("/metrics", make_asgi_app())

    app.include_router(health_router.router)
    for router in API_ROUTERS:
        app.include_router(router, prefix=settings.api_prefix)

    return app


app = create_app()
