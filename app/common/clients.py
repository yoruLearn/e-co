"""外部客户端装配（MySQL / Redis / HTTP）。

设计要点：

- **懒连接**：``create_async_engine`` 与 ``Redis.from_url`` 都不会在构造时建立网络连接，
  因此依赖未就绪时 api 进程仍能启动，就绪与否交给 ``/readyz``（见 ADR-0003）；
- **单例**：随 lifespan 创建/关闭，路由通过 ``request.app.state.clients`` 取用；
- 连接池参数按 Spec §15 容量目标（双副本 100 QPS 问答）预留。
"""

from __future__ import annotations

from dataclasses import dataclass

import httpx
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.common.config import Settings


@dataclass(slots=True)
class AppClients:
    """进程级客户端容器。"""

    settings: Settings
    engine: AsyncEngine
    session_factory: async_sessionmaker[AsyncSession]
    redis: Redis
    http: httpx.AsyncClient

    async def aclose(self) -> None:
        await self.http.aclose()
        await self.redis.aclose()
        await self.engine.dispose()


def build_clients(settings: Settings) -> AppClients:
    timeout_s = max(settings.ready_timeout_ms / 1000, 0.5)
    engine = create_async_engine(
        settings.database_url,
        pool_pre_ping=True,
        pool_size=10,
        max_overflow=10,
        pool_recycle=1800,
        echo=False,
        connect_args={"connect_timeout": int(max(timeout_s, 1))},
    )
    session_factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    redis: Redis = Redis.from_url(
        settings.redis_url,
        encoding="utf-8",
        decode_responses=True,
        socket_connect_timeout=timeout_s,
        socket_timeout=timeout_s,
        health_check_interval=30,
    )
    http = httpx.AsyncClient(
        timeout=httpx.Timeout(timeout_s, connect=timeout_s),
        headers={"user-agent": f"{settings.app_name}/{settings.app_version}"},
    )
    return AppClients(
        settings=settings,
        engine=engine,
        session_factory=session_factory,
        redis=redis,
        http=http,
    )
