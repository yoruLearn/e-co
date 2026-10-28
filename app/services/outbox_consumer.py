"""Outbox 消费者（Spec §12.3）：幂等消费 + 标记 consumed。

- 幂等：``SET NX consumed:{msg_id}``（Redis 不可用时退化为进程内 set）；
- 业务处理：M2 只做"工单创建/审批事件"的最小落点（日志 + 标记），
  真实下游（通知、工单系统）在 M4 联调时替换 handler。
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from typing import Any

from redis.asyncio import Redis
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.common.logging import get_logger, log_event, log_warning
from app.db.models import OutboxMessage
from app.services.outbox import Topic

logger = get_logger("app.services.outbox_consumer")

Handler = Callable[[str, dict[str, Any]], Awaitable[None]]


class OutboxConsumer:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        redis: Redis | None,
        *,
        handlers: dict[str, Handler] | None = None,
    ) -> None:
        self._sessions = session_factory
        self._redis = redis
        self._handlers = handlers or {}
        self._seen: set[str] = set()

    async def _first_time(self, msg_id: str) -> bool:
        if self._redis is not None:
            try:
                created = await self._redis.set(f"consumed:{msg_id}", "1", nx=True, ex=86_400)
                return bool(created)
            except Exception as exc:
                log_warning(logger, "consumer.dedup.failed", reason=type(exc).__name__)
        if msg_id in self._seen:
            return False
        self._seen.add(msg_id)
        if len(self._seen) > 10_000:
            self._seen.clear()
        return True

    async def consume(self, msg_id: str, topic: str, payload: dict[str, Any]) -> bool:
        if not await self._first_time(msg_id):
            return False
        handler = self._handlers.get(topic)
        if handler is not None:
            await handler(topic, payload)
        log_event(logger, "outbox.consumed", msg_id=msg_id, topic=topic)
        async with self._sessions() as session, session.begin():
            await session.execute(
                update(OutboxMessage)
                .where(OutboxMessage.msg_id == msg_id)
                .values(status="consumed")
            )
        return True

    async def consume_ticket(self, topic: str, payload: dict[str, Any]) -> None:
        logger.info(
            "ticket.consumed",
            extra={"topic": topic, "payload": json.dumps(payload, ensure_ascii=False)[:500]},
        )


def default_handlers() -> dict[str, Handler]:
    async def _noop(topic: str, payload: dict[str, Any]) -> None:
        return None

    return {str(Topic.TICKET_CREATE): _noop, str(Topic.APPROVAL_EVENT): _noop}
