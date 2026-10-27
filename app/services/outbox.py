"""Outbox 消息（Spec §12.2 信封 + §12.3 投递契约）。

M0 只做**信封构造与重试策略**（纯函数），dispatcher 与消费者在 M2 实现：

- 事务内只写 ``outbox_message``，**不发 MQ**（§10.2 原则）；
- dispatcher 轮询 ``status='pending' AND next_retry_at<=now`` 投递，成功后置 ``published``；
- 发布失败指数退避 5s/15s/45s…，``retry>5`` 置 ``dead`` 并发 ``AS_DEAD_LETTER``。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Final
from uuid import uuid4

MAX_RETRY: Final[int] = 5
BASE_RETRY_SECONDS: Final[int] = 5
MAX_RETRY_SECONDS: Final[int] = 300


class OutboxStatus(StrEnum):
    PENDING = "pending"
    PUBLISHED = "published"
    CONSUMED = "consumed"
    DEAD = "dead"


class Topic(StrEnum):
    TICKET_CREATE = "AS_TICKET_CREATE"
    APPROVAL_EVENT = "AS_APPROVAL_EVENT"
    NOTIFY = "AS_NOTIFY"
    EVAL_REPORT = "AS_EVAL_REPORT"
    TRACE_LOG = "AS_TRACE_LOG"
    DEAD_LETTER = "AS_DEAD_LETTER"


class MessageSchema(StrEnum):
    TICKET_CREATE_V1 = "as.ticket.create.v1"
    APPROVAL_EVENT_V1 = "as.approval.event.v1"
    NOTIFY_V1 = "as.notify.v1"


def new_msg_id() -> str:
    return f"msg_{uuid4().hex}"


def utc_now_iso(now: datetime | None = None) -> str:
    moment = now or datetime.now(tz=UTC)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass(frozen=True, slots=True)
class MessageEnvelope:
    """§12.2 统一信封。"""

    msg_id: str
    biz_id: str
    trace_id: str
    occurred_at: str
    schema: str
    payload: dict[str, Any]

    @property
    def sharding_key(self) -> str:
        """同 biz_id 哈希分区，保证单据内事件有序。"""
        return self.biz_id

    def as_dict(self) -> dict[str, Any]:
        return {
            "msg_id": self.msg_id,
            "biz_id": self.biz_id,
            "trace_id": self.trace_id,
            "occurred_at": self.occurred_at,
            "schema": self.schema,
            "payload": self.payload,
        }

    def to_json(self) -> str:
        return json.dumps(self.as_dict(), ensure_ascii=False, sort_keys=True)

    @property
    def payload_hash(self) -> str:
        return hashlib.sha256(self.to_json().encode("utf-8")).hexdigest()


def build_envelope(
    *,
    biz_id: str,
    trace_id: str,
    schema: MessageSchema | str,
    payload: dict[str, Any],
    occurred_at: datetime | None = None,
    msg_id: str | None = None,
) -> MessageEnvelope:
    return MessageEnvelope(
        msg_id=msg_id or new_msg_id(),
        biz_id=biz_id,
        trace_id=trace_id,
        occurred_at=utc_now_iso(occurred_at),
        schema=str(schema),
        payload=payload,
    )


def validate_envelope(raw: dict[str, Any]) -> MessageEnvelope:
    """消费端校验：字段缺失/类型不符直接拒绝，避免脏消息进入业务。"""
    required = ("msg_id", "biz_id", "trace_id", "occurred_at", "schema", "payload")
    missing = [key for key in required if key not in raw]
    if missing:
        msg = f"消息信封印第缺少字段: {missing}"
        raise ValueError(msg)
    payload = raw["payload"]
    if not isinstance(payload, dict):
        msg = "payload 必须是 JSON object"
        raise TypeError(msg)
    return MessageEnvelope(
        msg_id=str(raw["msg_id"]),
        biz_id=str(raw["biz_id"]),
        trace_id=str(raw["trace_id"]),
        occurred_at=str(raw["occurred_at"]),
        schema=str(raw["schema"]),
        payload=payload,
    )


def retry_delay_seconds(retry: int) -> int:
    """指数退避：0→5s, 1→15s, 2→45s, 3→135s, 4→300s（封顶）。"""
    if retry < 0:
        raise ValueError("retry 不能为负")
    delay: int = BASE_RETRY_SECONDS * int(3**retry)
    return int(min(delay, MAX_RETRY_SECONDS))


def should_dead_letter(retry: int, max_retry: int = MAX_RETRY) -> bool:
    return retry > max_retry


def next_retry_at(retry: int, now: datetime | None = None) -> datetime:
    from datetime import timedelta

    moment = now or datetime.now(tz=UTC)
    return moment + timedelta(seconds=retry_delay_seconds(retry))
