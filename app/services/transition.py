"""状态迁移唯一入口（Spec §5.2 约束 1/2）。

所有业务服务（售后单、审批回调、worker）都必须调用 ``transition_biz``，
禁止各自写 ``UPDATE ... SET status=``。
"""

from __future__ import annotations

from typing import Any, cast

from sqlalchemy import update
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.errors import AppError, ErrorCode
from app.db.models import AfterSalesBiz, AfterSalesTransition
from app.services.state_machine import AfterSalesEvent, AfterSalesStatus, apply_event


async def transition_biz(
    session: AsyncSession,
    biz: AfterSalesBiz,
    event: AfterSalesEvent,
    *,
    actor: str,
    trace_id: str,
) -> AfterSalesStatus:
    """校验迁移表 → 乐观锁 UPDATE → 写流水；冲突抛 ``AS-4003``。"""
    current = AfterSalesStatus(biz.status)
    target = apply_event(biz_id=biz.biz_id, current=current, event=event)
    if target is None:
        raise AppError(
            ErrorCode.BUSINESS_RULE,
            "该事件不产生状态跃迁",
            detail={"event": str(event)},
        )
    result = cast(
        CursorResult[Any],
        await session.execute(
            update(AfterSalesBiz)
            .where(
                AfterSalesBiz.biz_id == biz.biz_id,
                AfterSalesBiz.version == biz.version,
                AfterSalesBiz.status == str(current),
            )
            .values(status=str(target), version=biz.version + 1)
        ),
    )
    if result.rowcount != 1:
        raise AppError(
            ErrorCode.ILLEGAL_TRANSITION,
            "状态版本冲突，请重读后重试",
            detail={"biz_id": biz.biz_id, "expected_version": biz.version},
        )
    session.add(
        AfterSalesTransition(
            biz_id=biz.biz_id,
            from_status=str(current),
            to_status=str(target),
            event=str(event),
            actor=actor,
            version=biz.version + 1,
            trace_id=trace_id,
        )
    )
    biz.version += 1
    biz.status = str(target)
    return target
