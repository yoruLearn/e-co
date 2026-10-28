"""售后单服务：状态机唯一入口 + 乐观锁 + 流水 + outbox（Spec §5.2 / §10.2 / FR-AS-02/03/05）。

事务边界（一个事务内完成，失败全回滚）：

1. ``aftersales_biz`` 插入（幂等键唯一）；
2. ``DRAFT --SUBMIT--> PENDING_APPROVAL``（乐观锁 ``version``）；
3. 金额 > 阈值 → 建 ``approval_task``；否则自动 ``APPROVE``（actor=system）；
4. 写 ``aftersales_transition`` 流水；
5. 写 ``outbox_message``（同事务，事务外才允许发 MQ）。

禁止在别处直接 ``UPDATE status``：状态变化必须经过 ``transition_biz``。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Literal
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.api.errors import AppError, ErrorCode, IdempotencyConflictError
from app.common.config import Settings
from app.common.logging import get_logger, log_event
from app.db.models import AfterSalesBiz, AfterSalesTransition, ApprovalTask, OutboxMessage
from app.services.approval import (
    approval_expiry,
    new_approval_task_id,
    plan_approval,
)
from app.services.outbox import MessageSchema, Topic, build_envelope
from app.services.state_machine import AfterSalesEvent, AfterSalesStatus, is_terminal
from app.services.transition import transition_biz

logger = get_logger("app.services.aftersales")

AfterSalesAction = Literal["refund", "return", "exchange"]


def new_biz_id(now: datetime | None = None) -> str:
    moment = now or datetime.now(tz=UTC)
    return f"AS{moment:%Y%m%d}{uuid4().hex[:6].upper()}"


@dataclass(frozen=True, slots=True)
class ApplyCommand:
    user_id: str
    order_id: str
    action: AfterSalesAction
    reason_code: str
    amount: Decimal | None
    trace_id: str
    idem_key: str


@dataclass(frozen=True, slots=True)
class ApplyOutcome:
    biz_id: str
    status: str
    approval_required: bool
    task_id: str | None


@dataclass(frozen=True, slots=True)
class TransitionOutcome:
    biz_id: str
    status: str
    version: int


def _same_params(biz: AfterSalesBiz, cmd: ApplyCommand) -> bool:
    return (
        biz.order_id == cmd.order_id
        and biz.action == cmd.action
        and biz.reason_code == cmd.reason_code
        and (biz.amount or Decimal("0")) == (cmd.amount or Decimal("0"))
    )


class AfterSalesService:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        settings: Settings,
    ) -> None:
        self._sessions = session_factory
        self._settings = settings

    async def apply(self, cmd: ApplyCommand) -> ApplyOutcome:
        async with self._sessions() as session, session.begin():
            existing = await session.scalar(
                select(AfterSalesBiz).where(AfterSalesBiz.idem_key == cmd.idem_key)
            )
            if existing is not None:
                if not _same_params(existing, cmd):
                    raise IdempotencyConflictError(
                        cmd.idem_key,
                        detail={"biz_id": existing.biz_id},
                    )
                return ApplyOutcome(
                    biz_id=existing.biz_id,
                    status=existing.status,
                    approval_required=existing.status == str(AfterSalesStatus.PENDING_APPROVAL),
                    task_id=None,
                )

            biz_id = new_biz_id()
            biz = AfterSalesBiz(
                biz_id=biz_id,
                user_id=cmd.user_id,
                order_id=cmd.order_id,
                action=cmd.action,
                status=str(AfterSalesStatus.DRAFT),
                amount=cmd.amount,
                reason_code=cmd.reason_code,
                idem_key=cmd.idem_key,
                version=0,
                trace_id=cmd.trace_id,
            )
            session.add(biz)
            await session.flush()

            status = await transition_biz(
                session,
                biz,
                AfterSalesEvent.SUBMIT,
                actor="agent",
                trace_id=cmd.trace_id,
            )
            plan = plan_approval(cmd.amount, self._settings)
            approval_required = plan.required
            task_id: str | None = None
            if approval_required:
                task_id = new_approval_task_id()
                session.add(
                    ApprovalTask(
                        task_id=task_id,
                        biz_id=biz_id,
                        status="pending",
                        threshold=plan.threshold,
                        expires_at=approval_expiry(self._settings).replace(tzinfo=None),
                    )
                )
                final_status = status
            else:
                final_status = await transition_biz(
                    session,
                    biz,
                    AfterSalesEvent.APPROVE,
                    actor="system",
                    trace_id=cmd.trace_id,
                )
                approval_required = False

            envelope = build_envelope(
                biz_id=biz_id,
                trace_id=cmd.trace_id,
                schema=MessageSchema.TICKET_CREATE_V1,
                payload={
                    "biz_id": biz_id,
                    "action": cmd.action,
                    "status": str(final_status),
                    "order_id": cmd.order_id,
                    "amount": str(cmd.amount) if cmd.amount is not None else None,
                },
            )
            session.add(
                OutboxMessage(
                    msg_id=envelope.msg_id,
                    topic=str(Topic.TICKET_CREATE),
                    sharding_key=biz_id,
                    payload=envelope.as_dict(),
                    status="pending",
                    retry=0,
                    next_retry_at=datetime.now(tz=UTC).replace(tzinfo=None),
                    trace_id=cmd.trace_id,
                )
            )

        log_event(
            logger,
            "aftersales.applied",
            biz_id=biz_id,
            status=str(final_status),
            approval_required=approval_required,
            operator=cmd.user_id,
        )
        return ApplyOutcome(
            biz_id=biz_id,
            status=str(final_status),
            approval_required=approval_required,
            task_id=task_id,
        )

    async def get(self, biz_id: str, *, user_id: str, role: str = "buyer") -> dict[str, Any]:
        async with self._sessions() as session:
            biz = await session.scalar(select(AfterSalesBiz).where(AfterSalesBiz.biz_id == biz_id))
            if biz is None:
                raise AppError(ErrorCode.NOT_FOUND, "售后单不存在", detail={"biz_id": biz_id})
            if role == "buyer" and biz.user_id != user_id:
                raise AppError(ErrorCode.FORBIDDEN, "无权查看他人售后单")
            transitions = (
                await session.scalars(
                    select(AfterSalesTransition)
                    .where(AfterSalesTransition.biz_id == biz_id)
                    .order_by(AfterSalesTransition.id.asc())
                )
            ).all()
        return {
            "biz_id": biz.biz_id,
            "action": biz.action,
            "status": biz.status,
            "version": biz.version,
            "order_id": biz.order_id,
            "amount": float(biz.amount) if biz.amount is not None else None,
            "created_at": biz.created_at,
            "timeline": [{"status": row.to_status, "at": row.created_at} for row in transitions],
        }

    async def cancel(
        self, biz_id: str, *, user_id: str, role: str = "buyer", trace_id: str = ""
    ) -> TransitionOutcome:
        async with self._sessions() as session, session.begin():
            biz = await session.scalar(select(AfterSalesBiz).where(AfterSalesBiz.biz_id == biz_id))
            if biz is None:
                raise AppError(ErrorCode.NOT_FOUND, "售后单不存在", detail={"biz_id": biz_id})
            if role == "buyer" and biz.user_id != user_id:
                raise AppError(ErrorCode.FORBIDDEN, "无权取消他人售后单")
            if is_terminal(AfterSalesStatus(biz.status)):
                raise AppError(
                    ErrorCode.BUSINESS_RULE,
                    "终态单据不可取消",
                    detail={"status": biz.status},
                )
            status = await transition_biz(
                session,
                biz,
                AfterSalesEvent.CANCEL,
                actor=user_id,
                trace_id=trace_id,
            )
        return TransitionOutcome(biz_id=biz_id, status=str(status), version=biz.version)

    async def approve_or_reject(
        self,
        biz_id: str,
        *,
        approve: bool,
        actor: str,
        trace_id: str,
    ) -> TransitionOutcome:
        async with self._sessions() as session, session.begin():
            biz = await session.scalar(select(AfterSalesBiz).where(AfterSalesBiz.biz_id == biz_id))
            if biz is None:
                raise AppError(ErrorCode.NOT_FOUND, "售后单不存在", detail={"biz_id": biz_id})
            event = AfterSalesEvent.APPROVE if approve else AfterSalesEvent.REJECT
            status = await transition_biz(session, biz, event, actor=actor, trace_id=trace_id)
        return TransitionOutcome(biz_id=biz_id, status=str(status), version=biz.version)
