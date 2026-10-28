"""M2 服务单元测试：确认令牌、幂等存储、HMAC、只读工具。"""

from __future__ import annotations

import asyncio
from decimal import Decimal
from typing import Any

import pytest

from app.api.errors import AppError
from app.common.config import Settings
from app.services.approval import (
    compute_signature,
    needs_manual_approval,
    plan_approval,
    verify_signature,
)
from app.services.confirm import ConfirmTokenService
from app.services.idempotency_store import RedisIdempotencyStore, StoreDecision
from app.tools.orders import MockOrderProvider, OrderTools


class FakeRedis:
    """只实现 M2 用到的命令；``getdel`` 模拟 Redis 6.2 语义。"""

    def __init__(self) -> None:
        self.data: dict[str, str] = {}
        self.fail = False

    async def set(
        self, key: str, value: str, *, nx: bool = False, ex: int | None = None
    ) -> bool | None:
        if self.fail:
            raise ConnectionError("down")
        if nx and key in self.data:
            return None
        self.data[key] = value
        return True

    async def get(self, key: str) -> str | None:
        if self.fail:
            raise ConnectionError("down")
        return self.data.get(key)

    async def getdel(self, key: str) -> str | None:
        if self.fail:
            raise ConnectionError("down")
        return self.data.pop(key, None)

    async def delete(self, *keys: str) -> int:
        return sum(1 for key in keys if self.data.pop(key, None) is not None)


async def test_confirm_token_happy_path_and_single_use() -> None:
    service = ConfirmTokenService(FakeRedis())  # type: ignore[arg-type]
    token = await service.issue(
        user_id="u1",
        action="refund",
        order_id="O1",
        amount=Decimal("199.00"),
        trace_id="tr_1",
    )
    assert token.startswith("ct_")
    payload = await service.consume(
        token, user_id="u1", action="refund", order_id="O1", amount=Decimal("199.0")
    )
    assert payload.order_id == "O1"
    with pytest.raises(AppError) as exc:
        await service.consume(
            token, user_id="u1", action="refund", order_id="O1", amount=Decimal("199")
        )
    assert exc.value.code == "AS-4001"


async def test_confirm_token_rejects_param_swap() -> None:
    service = ConfirmTokenService(FakeRedis())  # type: ignore[arg-type]
    token = await service.issue(
        user_id="u1", action="refund", order_id="O1", amount=None, trace_id="tr"
    )
    with pytest.raises(AppError):
        await service.consume(token, user_id="u1", action="refund", order_id="O2", amount=None)


async def test_confirm_token_rejects_other_user() -> None:
    service = ConfirmTokenService(FakeRedis())  # type: ignore[arg-type]
    token = await service.issue(
        user_id="u1", action="refund", order_id="O1", amount=None, trace_id="tr"
    )
    with pytest.raises(AppError):
        await service.consume(token, user_id="u2", action="refund", order_id="O1", amount=None)


async def test_confirm_token_redis_failure_fails_closed() -> None:
    redis = FakeRedis()
    redis.fail = True
    service = ConfirmTokenService(redis)  # type: ignore[arg-type]
    with pytest.raises(AppError) as exc:
        await service.issue(
            user_id="u1", action="refund", order_id="O1", amount=None, trace_id="tr"
        )
    assert exc.value.code == "AS-5032"


async def test_idempotency_new_then_replay() -> None:
    store = RedisIdempotencyStore(FakeRedis())  # type: ignore[arg-type]
    first = await store.begin("k1", "hash-a")
    assert first.decision is StoreDecision.NEW
    await store.complete("k1", {"biz_id": "AS1"})
    replay = await store.begin("k1", "hash-a")
    assert replay.decision is StoreDecision.REPLAY
    assert replay.response == {"biz_id": "AS1"}


async def test_idempotency_conflict_on_different_params() -> None:
    store = RedisIdempotencyStore(FakeRedis())  # type: ignore[arg-type]
    await store.begin("k1", "hash-a")
    conflict = await store.begin("k1", "hash-b")
    assert conflict.decision is StoreDecision.CONFLICT


async def test_idempotency_pending_when_first_not_finished(monkeypatch) -> None:
    import app.services.idempotency_store as module

    monkeypatch.setattr(module, "POLL_ATTEMPTS", 2)
    monkeypatch.setattr(module, "POLL_INTERVAL_S", 0.0)
    store = RedisIdempotencyStore(FakeRedis())  # type: ignore[arg-type]
    await store.begin("k1", "hash-a")
    pending = await store.begin("k1", "hash-a")
    assert pending.decision is StoreDecision.PENDING


async def test_idempotency_without_redis_is_unknown() -> None:
    store = RedisIdempotencyStore(None)
    assert (await store.begin("k1", "h")).decision is StoreDecision.UNKNOWN


def test_hmac_signature_roundtrip() -> None:
    body = b'{"decision":"approve"}'
    signature = compute_signature("secret", body)
    assert verify_signature("secret", body, signature) is True
    assert verify_signature("secret", body, signature.upper()) is True
    assert verify_signature("secret", body, "deadbeef") is False
    assert verify_signature("secret", body, None) is False


def test_approval_threshold_boundary() -> None:
    assert needs_manual_approval(Decimal("500.00"), Decimal("500")) is False
    assert needs_manual_approval(Decimal("500.01"), Decimal("500")) is True
    assert needs_manual_approval(None, Decimal("500")) is False
    settings = Settings(_env_file=None, approval_threshold_cny=500)
    plan = plan_approval(Decimal("600"), settings)
    assert plan.required is True
    assert plan.threshold == Decimal("500.0")


async def test_order_tools_ownership_and_lookup() -> None:
    tools = OrderTools(MockOrderProvider())
    own = await tools.get_order("O20261027001", user_id="u_buyer_1", role="buyer")
    assert own.ok and own.data["order_id"] == "O20261027001"
    with pytest.raises(AppError) as exc:
        await tools.get_order("O20261027001", user_id="u_buyer_2", role="buyer")
    assert exc.value.code == "AS-4030"
    with pytest.raises(AppError) as exc2:
        await tools.get_order("O404", user_id="u_buyer_1", role="buyer")
    assert exc2.value.code == "AS-4040"
    agent_view = await tools.get_order("O20261027001", user_id="u_agent", role="agent")
    assert agent_view.ok


async def test_order_tools_stock_and_timeout() -> None:
    tools = OrderTools(MockOrderProvider())
    stock = await tools.get_stock("SKU-BT-PRO", qty=2)
    assert stock.ok and stock.data["enough"] is True
    sold_out = await tools.get_stock("SKU-CASE", qty=1)
    assert sold_out.ok and sold_out.data["enough"] is False

    class SlowProvider(MockOrderProvider):
        async def get_order(self, order_id: str) -> Any:
            await asyncio.sleep(0.05)
            return await super().get_order(order_id)

    slow = OrderTools(SlowProvider(), timeout_s=0.001)
    degraded = await slow.get_order("O20261027001", user_id="u_buyer_1")
    assert degraded.ok is False
    assert degraded.degraded is True
    assert degraded.error_code == "TOOL_TIMEOUT"
