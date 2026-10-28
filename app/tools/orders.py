"""只读业务工具（Spec §8.1 / FR-AS-01）。

- 统一 ``ToolResult``：成功/降级/失败都不抛穿图（除越权 → 403）；
- 单工具超时：订单/物流/退款 800ms、库存 500ms；超时返回 ``degraded`` 提示；
- 分级授权：buyer 只能查本人订单，agent 可查任意（写审计由上层负责）；
- M2 用 ``MockOrderProvider``；真实 Provider 在 M4 联调，接口边界已冻结。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Literal, Protocol

from app.api.errors import AppError, ErrorCode
from app.common.logging import get_logger, log_warning

logger = get_logger("app.tools.orders")

ORDER_TIMEOUT_S = 0.8
STOCK_TIMEOUT_S = 0.5


@dataclass(frozen=True, slots=True)
class OrderLine:
    sku_id: str
    title: str
    qty: int
    price: Decimal


@dataclass(frozen=True, slots=True)
class Order:
    order_id: str
    user_id: str
    status: str
    amount: Decimal
    created_at: str
    lines: list[OrderLine] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class LogisticsEvent:
    at: str
    status: str
    description: str


@dataclass(frozen=True, slots=True)
class ToolResult:
    ok: bool
    data: Any = None
    error_code: str | None = None
    degraded: bool = False
    cost_ms: int = 0


class OrderProvider(Protocol):
    async def get_order(self, order_id: str) -> Order | None: ...

    async def get_logistics(self, order_id: str) -> list[LogisticsEvent]: ...

    async def get_refund_progress(self, order_id: str) -> dict[str, Any] | None: ...

    async def get_stock(self, sku_id: str) -> int: ...


class MockOrderProvider:
    """固定 fixtures：与 Spec 示例订单对齐，便于评测与联调前联调。"""

    def __init__(self) -> None:
        self.orders: dict[str, Order] = {
            "O20261027001": Order(
                order_id="O20261027001",
                user_id="u_buyer_1",
                status="SIGNED",
                amount=Decimal("199.00"),
                created_at="2026-10-20T10:00:00Z",
                lines=[
                    OrderLine(
                        sku_id="SKU-BT-PRO",
                        title="蓝牙耳机 Pro",
                        qty=1,
                        price=Decimal("199.00"),
                    )
                ],
            ),
            "O20261027002": Order(
                order_id="O20261027002",
                user_id="u_buyer_2",
                status="SHIPPED",
                amount=Decimal("89.00"),
                created_at="2026-10-25T09:00:00Z",
                lines=[
                    OrderLine(
                        sku_id="SKU-CASE",
                        title="耳机保护套",
                        qty=2,
                        price=Decimal("44.50"),
                    )
                ],
            ),
        }
        self.stock: dict[str, int] = {"SKU-BT-PRO": 42, "SKU-CASE": 0}

    async def get_order(self, order_id: str) -> Order | None:
        return self.orders.get(order_id)

    async def get_logistics(self, order_id: str) -> list[LogisticsEvent]:
        return [
            LogisticsEvent(at="2026-10-21T09:00:00Z", status="SHIPPED", description="已揽收"),
            LogisticsEvent(at="2026-10-22T15:30:00Z", status="SIGNED", description="已签收"),
        ]

    async def get_refund_progress(self, order_id: str) -> dict[str, Any] | None:
        if order_id not in self.orders:
            return None
        return {"stage": "refund_received", "eta": "1 个工作日", "order_id": order_id}

    async def get_stock(self, sku_id: str) -> int:
        return self.stock.get(sku_id, 0)


class OrderTools:
    def __init__(self, provider: OrderProvider, *, timeout_s: float = ORDER_TIMEOUT_S) -> None:
        self._provider = provider
        self._timeout = timeout_s

    async def _timed(self, coro: Any, timeout_seconds: float) -> ToolResult:
        started = asyncio.get_running_loop().time()
        try:
            data = await asyncio.wait_for(coro, timeout=timeout_seconds)
        except TimeoutError:
            return ToolResult(ok=False, degraded=True, error_code="TOOL_TIMEOUT")
        except Exception as exc:  # 工具失败降级，不打断对话
            log_warning(logger, "tool.failed", reason=type(exc).__name__)
            return ToolResult(ok=False, degraded=True, error_code="TOOL_ERROR")
        cost_ms = int((asyncio.get_running_loop().time() - started) * 1000)
        return ToolResult(ok=True, data=data, cost_ms=cost_ms)

    async def get_order(self, order_id: str, *, user_id: str, role: str = "buyer") -> ToolResult:
        result = await self._timed(self._provider.get_order(order_id), self._timeout)
        if not result.ok:
            return result
        order: Order | None = result.data
        if order is None:
            raise AppError(ErrorCode.NOT_FOUND, "订单不存在", detail={"order_id": order_id})
        if role == "buyer" and order.user_id != user_id:
            raise AppError(ErrorCode.FORBIDDEN, "无权查看他人订单", detail={"order_id": order_id})
        return ToolResult(
            ok=True,
            cost_ms=result.cost_ms,
            data={
                "order_id": order.order_id,
                "user_id": order.user_id,
                "status": order.status,
                "amount": str(order.amount),
                "created_at": order.created_at,
                "lines": [
                    {
                        "sku_id": line.sku_id,
                        "title": line.title,
                        "qty": line.qty,
                        "price": str(line.price),
                        "amount": str(line.price * line.qty),
                    }
                    for line in order.lines
                ],
            },
        )

    async def get_logistics(self, order_id: str, *, role: str = "buyer") -> ToolResult:
        result = await self._timed(self._provider.get_logistics(order_id), self._timeout)
        if result.ok:
            result = ToolResult(
                ok=True,
                cost_ms=result.cost_ms,
                data={
                    "events": [
                        {"at": event.at, "status": event.status, "description": event.description}
                        for event in result.data
                    ]
                },
            )
        return result

    async def get_refund_progress(self, order_id: str, *, role: str = "buyer") -> ToolResult:
        result = await self._timed(self._provider.get_refund_progress(order_id), self._timeout)
        if result.ok and result.data is None:
            return ToolResult(ok=False, error_code="NO_REFUND", degraded=True)
        return result

    async def get_stock(self, sku_id: str, qty: int = 1) -> ToolResult:
        result = await self._timed(self._provider.get_stock(sku_id), STOCK_TIMEOUT_S)
        if not result.ok:
            return result
        available = int(result.data)
        return ToolResult(
            ok=True,
            cost_ms=result.cost_ms,
            data={"sku_id": sku_id, "available": available, "enough": available >= qty},
        )


ToolName = Literal["get_order", "get_logistics", "get_refund_progress", "get_stock"]
