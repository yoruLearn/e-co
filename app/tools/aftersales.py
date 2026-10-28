"""写工具：受理 / 取消售后（Spec §8.1 ``create_after_sales`` / ``cancel_after_sales``）。

写工具是"确认令牌 + 幂等 + 状态机 + 审计"的组合层：
- 调用前必须消费 ``confirm_token``（缺失/过期/换参 → AS-4001）；
- 幂等键由用户请求携带（``Idempotency-Key``），同一键重放返回首次结果；
- 审计记录 params_hash 与结果码，绝不记录明文 token。
"""

from __future__ import annotations

import time
from decimal import Decimal

from app.api.errors import AppError
from app.common.logging import get_logger
from app.services.aftersales import AfterSalesService, ApplyCommand, ApplyOutcome, TransitionOutcome
from app.services.confirm import ConfirmTokenService
from app.services.tool_audit import NullToolAudit, ToolAuditRecorder

logger = get_logger("app.tools.aftersales")


class AfterSalesTools:
    def __init__(
        self,
        service: AfterSalesService,
        confirm: ConfirmTokenService,
        audit: ToolAuditRecorder | NullToolAudit | None = None,
    ) -> None:
        self._service = service
        self._confirm = confirm
        self._audit = audit or NullToolAudit()

    async def create(
        self,
        *,
        user_id: str,
        action: str,
        order_id: str,
        reason_code: str,
        amount: Decimal | None,
        confirm_token: str,
        idem_key: str,
        trace_id: str,
        role: str = "buyer",
    ) -> ApplyOutcome:
        started = time.perf_counter()
        await self._confirm.consume(
            confirm_token,
            user_id=user_id,
            action=action,
            order_id=order_id,
            amount=amount,
        )
        outcome = await self._service.apply(
            ApplyCommand(
                user_id=user_id,
                order_id=order_id,
                action=action,  # type: ignore[arg-type]
                reason_code=reason_code,
                amount=amount,
                trace_id=trace_id,
                idem_key=idem_key,
            )
        )
        await self._audit.record(
            trace_id=trace_id,
            user_id=user_id,
            tool="create_after_sales",
            level="write",
            params={
                "order_id": order_id,
                "action": action,
                "amount": str(amount or ""),
                "role": role,
            },
            result_code="OK",
            cost_ms=int((time.perf_counter() - started) * 1000),
        )
        return outcome

    async def cancel(
        self,
        *,
        biz_id: str,
        user_id: str,
        role: str,
        trace_id: str,
    ) -> TransitionOutcome:
        started = time.perf_counter()
        try:
            outcome = await self._service.cancel(
                biz_id, user_id=user_id, role=role, trace_id=trace_id
            )
        except AppError as exc:
            await self._audit.record(
                trace_id=trace_id,
                user_id=user_id,
                tool="cancel_after_sales",
                level="write",
                params={"biz_id": biz_id},
                result_code=str(exc.code),
                cost_ms=int((time.perf_counter() - started) * 1000),
            )
            raise
        await self._audit.record(
            trace_id=trace_id,
            user_id=user_id,
            tool="cancel_after_sales",
            level="write",
            params={"biz_id": biz_id},
            result_code="OK",
            cost_ms=int((time.perf_counter() - started) * 1000),
        )
        return outcome
