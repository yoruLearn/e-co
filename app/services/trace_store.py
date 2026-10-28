"""M1: 节点级调用树（Spec §13.4 / ``GET /traces/{id}``）。

每个图节点执行后落一条 span：起点（epoch ms）、耗时、状态、token、附加信息。
写入是**尽力而为**：trace 落库失败只告警，绝不影响对话主链路。

为什么用独立表而不是只靠日志：日志适合排查单次故障，trace 表适合
"按 trace_id 还原一次请求的节点耗时分布"，并在 M3 做慢节点聚合。
"""

from __future__ import annotations

import time
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.common.logging import get_logger, log_warning
from app.db.models import TraceSpan

logger = get_logger("app.services.trace_store")


class NullTraceRecorder:
    """离线/测试用：不落库，接口返回空。"""

    async def record(self, **kwargs: Any) -> None:
        return None

    async def fetch(self, trace_id: str) -> list[dict[str, Any]]:
        return []


class TraceRecorder:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def record(
        self,
        *,
        trace_id: str,
        node: str,
        start_ms: int | None = None,
        duration_ms: int = 0,
        status: str = "ok",
        tokens: int = 0,
        meta: dict[str, Any] | None = None,
    ) -> None:
        if not trace_id:
            return
        now_ms = int(time.time() * 1000)
        try:
            async with self._session_factory() as session:
                session.add(
                    TraceSpan(
                        trace_id=trace_id,
                        node=node,
                        start_ms=start_ms or now_ms,
                        duration_ms=max(0, duration_ms),
                        status=status,
                        tokens=max(0, tokens),
                        meta=meta or {},
                    )
                )
                await session.commit()
        except Exception as exc:  # trace 失败不影响主链路
            log_warning(logger, "trace.persist.failed", reason=type(exc).__name__, node=node)

    async def fetch(self, trace_id: str) -> list[dict[str, Any]]:
        async with self._session_factory() as session:
            rows = (
                await session.scalars(
                    select(TraceSpan)
                    .where(TraceSpan.trace_id == trace_id)
                    .order_by(TraceSpan.start_ms.asc(), TraceSpan.id.asc())
                )
            ).all()
        return [
            {
                "span": row.node,
                "start_ms": float(row.start_ms),
                "duration_ms": float(row.duration_ms),
                "status": row.status,
                "tokens": row.tokens,
                "meta": row.meta or {},
            }
            for row in rows
        ]
