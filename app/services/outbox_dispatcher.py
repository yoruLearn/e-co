"""Outbox dispatcher（Spec §12.3）：至少一次投递 + 指数退避 + 死信。

事务内只写 ``outbox_message``；本模块在事务外轮询投递：
- 成功 → ``published``；
- 失败 → ``retry+1``，``next_retry_at = now + min(5*2^retry, 300)s``；
- ``retry > 5`` → ``dead`` 并计 ``dlq_total``。
MQ 关闭时用 ``NullPublisher``：仍走完整状态机（本地消费/演练不丢消息语义）。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.common.logging import get_logger, log_warning
from app.common.metrics import DLQ_TOTAL
from app.db.models import OutboxMessage
from app.services.outbox import MAX_RETRY

logger = get_logger("app.services.outbox_dispatcher")

Publisher = Callable[[str, dict[str, Any]], Awaitable[bool]]


class MessagePublisher(Protocol):
    async def publish(self, topic: str, payload: dict[str, Any]) -> bool: ...


class NullPublisher:
    """MQ 未开启时使用：直接视为投递成功（消息语义仍可演练）。"""

    async def publish(self, topic: str, payload: dict[str, Any]) -> bool:
        return True


def retry_delay_seconds(retry: int) -> int:
    return int(min(5 * (2 ** max(0, retry)), 300))


class OutboxDispatcher:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        publisher: MessagePublisher | None = None,
        batch_size: int = 50,
        max_retry: int = MAX_RETRY,
    ) -> None:
        self._sessions = session_factory
        self._publisher = publisher or NullPublisher()
        self._batch = batch_size
        self._max_retry = max_retry

    async def dispatch_once(self, *, now: datetime | None = None) -> int:
        moment = now or datetime.now(tz=UTC).replace(tzinfo=None)
        published = 0
        async with self._sessions() as session, session.begin():
            rows = (
                await session.scalars(
                    select(OutboxMessage)
                    .where(
                        OutboxMessage.status == "pending",
                        OutboxMessage.next_retry_at <= moment,
                    )
                    .order_by(OutboxMessage.id.asc())
                    .limit(self._batch)
                )
            ).all()
            for row in rows:
                ok = False
                try:
                    ok = await self._publisher.publish(row.topic, row.payload or {})
                except Exception as exc:  # 投递失败进入重试
                    log_warning(
                        logger,
                        "outbox.publish.failed",
                        msg_id=row.msg_id,
                        reason=type(exc).__name__,
                    )
                if ok:
                    row.status = "published"
                    published += 1
                    continue
                row.retry += 1
                if row.retry > self._max_retry:
                    row.status = "dead"
                    DLQ_TOTAL.labels(topic=row.topic).inc()
                else:
                    row.next_retry_at = moment + timedelta(seconds=retry_delay_seconds(row.retry))
        return published
