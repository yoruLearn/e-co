"""写操作双确认令牌（Spec §9.2 / FR-HITL-01）。

- 由 ``confirm`` 事件签发，**单次消费**（Redis ``GETDEL``），TTL 300s；
- 绑定 ``user_id + action + order_id + amount``：换任何一个参数重放都失败；
- Redis 不可用 → 失败关闭（503 ``AS-5032``）：确认是写操作的正确性前提，不能放行。
"""

from __future__ import annotations

import json
import secrets
from dataclasses import asdict, dataclass
from decimal import Decimal
from typing import Any

from redis.asyncio import Redis

from app.api.errors import AppError, ErrorCode
from app.common.logging import get_logger, log_warning

logger = get_logger("app.services.confirm")

CONFIRM_PREFIX = "confirm:"
CONFIRM_TTL_SECONDS = 300


async def _maybe_await(value: Any) -> Any:
    if hasattr(value, "__await__"):
        return await value
    return value


@dataclass(frozen=True, slots=True)
class ConfirmPayload:
    user_id: str
    action: str
    order_id: str
    amount: str | None
    trace_id: str


def _amount_key(amount: Decimal | float | None) -> str | None:
    if amount is None:
        return None
    return f"{Decimal(str(amount)).quantize(Decimal('0.01'))}"


class ConfirmTokenService:
    def __init__(self, redis: Redis | None, *, ttl_seconds: int = CONFIRM_TTL_SECONDS) -> None:
        self._redis = redis
        self._ttl = max(30, ttl_seconds)

    @staticmethod
    def _key(token: str) -> str:
        return f"{CONFIRM_PREFIX}{token}"

    async def issue(
        self,
        *,
        user_id: str,
        action: str,
        order_id: str,
        amount: Decimal | float | None,
        trace_id: str,
    ) -> str:
        token = f"ct_{secrets.token_urlsafe(24)}"
        if self._redis is None:
            raise AppError(ErrorCode.DEPENDENCY_UNAVAILABLE, "确认令牌服务不可用")
        payload = ConfirmPayload(
            user_id=user_id,
            action=str(action),
            order_id=order_id,
            amount=_amount_key(amount),
            trace_id=trace_id,
        )
        try:
            await _maybe_await(
                self._redis.set(
                    self._key(token),
                    json.dumps(asdict(payload), ensure_ascii=False),
                    ex=self._ttl,
                )
            )
        except Exception as exc:
            log_warning(logger, "confirm.issue.failed", reason=type(exc).__name__)
            raise AppError(ErrorCode.DEPENDENCY_UNAVAILABLE, "确认令牌服务暂不可用") from exc
        return token

    async def consume(
        self,
        token: str,
        *,
        user_id: str,
        action: str,
        order_id: str,
        amount: Decimal | float | None,
    ) -> ConfirmPayload:
        if self._redis is None:
            raise AppError(ErrorCode.DEPENDENCY_UNAVAILABLE, "确认令牌服务不可用")
        try:
            raw = await _maybe_await(self._redis.getdel(self._key(token)))
        except Exception as exc:
            log_warning(logger, "confirm.consume.failed", reason=type(exc).__name__)
            raise AppError(ErrorCode.DEPENDENCY_UNAVAILABLE, "确认令牌服务暂不可用") from exc
        if not raw:
            raise AppError(
                ErrorCode.PARAM_INVALID,
                "确认令牌无效或已过期，请重新确认",
                detail={"field": "confirm_token"},
            )
        try:
            payload = json.loads(raw)
        except (json.JSONDecodeError, TypeError) as exc:
            raise AppError(ErrorCode.PARAM_INVALID, "确认令牌已损坏，请重新确认") from exc
        mismatch = (
            payload.get("user_id") != user_id
            or payload.get("action") != str(action)
            or payload.get("order_id") != order_id
            or payload.get("amount") != _amount_key(amount)
        )
        if mismatch:
            raise AppError(
                ErrorCode.PARAM_INVALID,
                "确认令牌与本次参数不匹配，请重新确认",
                detail={"field": "confirm_token"},
            )
        return ConfirmPayload(
            user_id=str(payload.get("user_id", "")),
            action=str(payload.get("action", "")),
            order_id=str(payload.get("order_id", "")),
            amount=payload.get("amount"),
            trace_id=str(payload.get("trace_id", "")),
        )
