"""业务工具清单（Spec §8.1）。

M0 只做**声明式注册表**：名称、分级、参数、超时、降级策略、是否需要双确认。
真实 Provider 调用（订单/物流/退款）在 M2 接入，M4 完成与上游契约测试（§21 Q1）。

之所以先把清单落成代码：LLM 结构化输出需要 schema 校验（§8.2 第 1 条），
而 schema 的权威来源必须是这份注册表，不能散落在 prompt 里。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Final


class ToolLevel(StrEnum):
    READ_ONLY = "ro"
    WRITE = "write"


@dataclass(frozen=True, slots=True)
class ToolSpec:
    name: str
    level: ToolLevel
    summary: str
    required_params: tuple[str, ...]
    optional_params: tuple[str, ...] = ()
    timeout_ms: int = 800
    degrade: str = ""
    requires_confirm: bool = False
    audit: bool = True
    idempotent: bool = False
    tags: tuple[str, ...] = field(default_factory=tuple)

    @property
    def params(self) -> tuple[str, ...]:
        return self.required_params + self.optional_params

    @property
    def is_write(self) -> bool:
        return self.level is ToolLevel.WRITE


#: Spec §8.1 工具清单 —— 唯一权威来源。
TOOL_SPECS: Final[tuple[ToolSpec, ...]] = (
    ToolSpec(
        name="get_order",
        level=ToolLevel.READ_ONLY,
        summary="查询订单头与行项目",
        required_params=("order_id",),
        timeout_ms=800,
        degrade="返回 60s 缓存快照",
        tags=("order",),
    ),
    ToolSpec(
        name="get_logistics",
        level=ToolLevel.READ_ONLY,
        summary="查询物流轨迹节点",
        required_params=("order_id",),
        timeout_ms=800,
        degrade="提示「轨迹延迟」",
        tags=("logistics",),
    ),
    ToolSpec(
        name="get_stock",
        level=ToolLevel.READ_ONLY,
        summary="查询 SKU 可用库存",
        required_params=("sku_id", "qty"),
        timeout_ms=500,
        degrade="使用 0.5s 缓存",
        tags=("inventory",),
    ),
    ToolSpec(
        name="get_refund_progress",
        level=ToolLevel.READ_ONLY,
        summary="查询退款阶段与预计时间",
        required_params=("order_id",),
        timeout_ms=800,
        degrade="引导用户前往进度页",
        tags=("refund",),
    ),
    ToolSpec(
        name="check_aftersales_policy",
        level=ToolLevel.READ_ONLY,
        summary="按类目与签收天数检索适用售后政策（RAG）",
        required_params=("category", "days"),
        timeout_ms=1200,
        degrade="返回兜底话术并要求转人工",
        tags=("policy", "rag"),
    ),
    ToolSpec(
        name="create_after_sales",
        level=ToolLevel.WRITE,
        summary="发起退货/换货/退款申请，返回 biz_id",
        required_params=("order_id", "action", "reason"),
        optional_params=("sku_id", "amount"),
        timeout_ms=1000,
        degrade="事务回滚，返回错误码",
        requires_confirm=True,
        idempotent=True,
        tags=("write", "biz"),
    ),
    ToolSpec(
        name="cancel_after_sales",
        level=ToolLevel.WRITE,
        summary="取消售后单",
        required_params=("biz_id",),
        timeout_ms=1000,
        degrade="事务回滚，返回错误码",
        requires_confirm=True,
        idempotent=True,
        tags=("write", "biz"),
    ),
)

TOOL_MAP: Final[dict[str, ToolSpec]] = {spec.name: spec for spec in TOOL_SPECS}

WRITE_TOOL_NAMES: Final[frozenset[str]] = frozenset(
    spec.name for spec in TOOL_SPECS if spec.is_write
)
READ_ONLY_TOOL_NAMES: Final[frozenset[str]] = frozenset(
    spec.name for spec in TOOL_SPECS if not spec.is_write
)


def get_tool(name: str) -> ToolSpec | None:
    return TOOL_MAP.get(name)


def missing_params(tool: ToolSpec, slots: dict[str, object]) -> list[str]:
    """缺参检测：值为 ``None``/空串视为缺失（供 FR-CHAT-03 澄清使用）。"""
    missing: list[str] = []
    for param in tool.required_params:
        value = slots.get(param)
        if value is None or (isinstance(value, str) and not value.strip()):
            missing.append(param)
    return missing
