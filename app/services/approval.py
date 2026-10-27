"""HITL 审批规则（Spec §3.3 / §8.1）。

M0 只落**判定规则**与任务标识生成；审批队列、回调验签、checkpoint 挂起在 M2 实现。

规则：退款金额 **严格大于** 阈值才进人工审批队列（阈值默认 500 元，可在线调整）。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from uuid import uuid4

from app.common.config import Settings


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
