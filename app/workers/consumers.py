"""MQ 消费者注册（Spec §12.3）。

M0 只提供**注册表与契约**：M2 接入 ``AS_TICKET_CREATE``（工单落库）、
``AS_APPROVAL_EVENT``（审批事件）、``AS_NOTIFY``（通知）。

消费契约（M2 实现时必须遵守）：

1. 消费幂等：``msg_id`` 先 SETNX（Redis）+ DB 唯一键，重复投递直接 ACK；
2. 本地重试 3 次 → 回退 broker（10s/30s/1m）→ 死信；
3. 同 ``sharding_key`` 有序；
4. 消费并发 = ``min(队列数, 配置阈值)``，保护 MySQL。
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Final

from app.services.outbox import MessageEnvelope, Topic

logger = logging.getLogger("app.worker.consumer")

Handler = Callable[[MessageEnvelope], Awaitable[None]]

#: M0 为空；M2 注册 ``Topic.TICKET_CREATE`` 等处理器。
HANDLERS: Final[dict[str, Handler]] = {}

LOCAL_RETRY_TIMES: Final[int] = 3
BROKER_RETRY_DELAYS_SEC: Final[tuple[int, ...]] = (10, 30, 60)


def register_handler(topic: Topic | str) -> Callable[[Handler], Handler]:
    def decorator(func: Handler) -> Handler:
        HANDLERS[str(topic)] = func
        return func

    return decorator


async def dispatch(envelope: MessageEnvelope) -> None:
    """按 topic 分发；无处理器视为编程错误（M2 前不应收到任何消息）。"""
    handler = HANDLERS.get(envelope.schema)
    if handler is None:
        msg = f"未注册的消息处理器: {envelope.schema}"
        raise LookupError(msg)
    await handler(envelope)


def missing_handlers(topics: list[Topic]) -> list[str]:
    return [str(topic) for topic in topics if str(topic) not in HANDLERS]
