"""HITL 审批：判定规则（M0）+ HMAC 验签/幂等回调（M2，Spec §3.3）。

规则：退款金额 **严格大于** 阈值才进人工审批队列（阈值默认 500 元，可在线调整）。

回调约定：
- ``X-Signature = HMAC-SHA256(secret, raw_body)``（hex），比较用 ``compare_digest``；
- 任务非 ``pending`` → 直接返回首次结果（``duplicated=True``），不重复推进状态；
- 状态推进复用唯一入口 ``transition_biz``（乐观锁），并写审批事件 outbox。
"""

from __future__ import annotations

import hashlib
import hmac
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from typing import Any
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.api.errors import AppError, ErrorCode
from app.common.config import Settings
from app.common.logging import get_logger, log_event
from app.db.models import AfterSalesBiz, ApprovalTask, OutboxMessage
from app.services.outbox import MessageSchema, Topic, build_envelope
from app.services.state_machine import AfterSalesEvent
from app.services.transition import transition_biz

logger = get_logger("app.services.approval")

SIGNATURE_HEADER = "X-Signature"


class ApprovalDecision(StrEnum):
    APPROVE = "approve"
    REJECT = "reject"


class ApprovalTaskStatus(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXPIRED = "expired"


@dataclass(frozen=True, slots=True)
class ApprovalPlan:
    required: bool
    threshold: Decimal
    reason: str

    def as_dict(self) -> dict[str, object]:
        return {
            "approval_required": self.required,
            "threshold_cny": float(self.threshold),
            "reason": self.reason,
        }


def needs_manual_approval(amount: float | Decimal | None, threshold: float | Decimal) -> bool:
    """金额 > 阈值才需人工审批；金额缺失（None）时不拦截自动流转。"""
    if amount is None:
        return False
    return Decimal(str(amount)) > Decimal(str(threshold))


def plan_approval(amount: float | Decimal | None, settings: Settings) -> ApprovalPlan:
    threshold = Decimal(str(settings.approval_threshold_cny))
    required = needs_manual_approval(amount, threshold)
    reason = "amount_exceeds_threshold" if required else "auto_approved"
    return ApprovalPlan(required=required, threshold=threshold, reason=reason)


def new_approval_task_id() -> str:
    return f"ap_{uuid4().hex[:24]}"


def approval_expiry(settings: Settings, now: datetime | None = None) -> datetime:
    moment = now or datetime.now(tz=UTC)
    return moment + timedelta(hours=settings.idle_approval_expire_hours)


def is_expired(expires_at: datetime, now: datetime | None = None) -> bool:
    moment = now or datetime.now(tz=UTC)
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=UTC)
    return moment >= expires_at


def compute_signature(secret: str, body: bytes) -> str:
    return hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()


def verify_signature(secret: str, body: bytes, signature: str | None) -> bool:
    if not signature:
        return False
    expected = compute_signature(secret, body)
    return hmac.compare_digest(expected, signature.strip().lower())


class ApprovalService:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        settings: Settings,
    ) -> None:
        self._sessions = session_factory
        self._settings = settings

    async def callback(
        self,
        *,
        task_id: str,
        decision: str,
        approver: str,
        comment: str | None,
        trace_id: str = "",
    ) -> dict[str, Any]:
        async with self._sessions() as session, session.begin():
            task = await session.scalar(select(ApprovalTask).where(ApprovalTask.task_id == task_id))
            if task is None:
                raise AppError(ErrorCode.NOT_FOUND, "审批任务不存在", detail={"task_id": task_id})
            biz = await session.scalar(
                select(AfterSalesBiz).where(AfterSalesBiz.biz_id == task.biz_id)
            )
            if biz is None:
                raise AppError(ErrorCode.NOT_FOUND, "售后单不存在", detail={"biz_id": task.biz_id})
            if task.status != "pending":
                log_event(
                    logger,
                    "approval.callback.duplicated",
                    task_id=task_id,
                    status=task.status,
                )
                return {
                    "approval_task_id": task.task_id,
                    "biz_id": task.biz_id,
                    "decision": decision,
                    "status": biz.status,
                    "duplicated": True,
                }

            approve = decision == "approve"
            event = AfterSalesEvent.APPROVE if approve else AfterSalesEvent.REJECT
            status = await transition_biz(session, biz, event, actor=approver, trace_id=trace_id)
            task.status = "approved" if approve else "rejected"
            task.approver = approver
            task.comment = comment
            task.callback_count += 1

            envelope = build_envelope(
                biz_id=task.biz_id,
                trace_id=trace_id,
                schema=MessageSchema.APPROVAL_EVENT_V1,
                payload={
                    "biz_id": task.biz_id,
                    "task_id": task.task_id,
                    "decision": decision,
                    "status": str(status),
                    "approver": approver,
                },
            )
            session.add(
                OutboxMessage(
                    msg_id=envelope.msg_id,
                    topic=str(Topic.APPROVAL_EVENT),
                    sharding_key=task.biz_id,
                    payload=envelope.as_dict(),
                    status="pending",
                    retry=0,
                    next_retry_at=datetime.now(tz=UTC).replace(tzinfo=None),
                    trace_id=trace_id,
                )
            )

        return {
            "approval_task_id": task_id,
            "biz_id": task.biz_id,
            "decision": decision,
            "status": str(status),
            "duplicated": False,
        }
