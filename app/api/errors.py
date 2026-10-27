"""统一错误码与异常（Spec §9.1）。

响应契约::

    {"code": "AS-4001", "message": "...", "detail": {...}, "trace_id": "tr_..."}

错误码清单与 Spec §9.1 表格严格对齐；``AS-5002`` 为本项目扩展（未实现端点专用），
避免把「M1/M2 未交付」混同为「内部错误 500」。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, NoReturn


class ErrorCode(StrEnum):
    """业务错误码。字符串值即对外 code。"""

    PARAM_INVALID = "AS-4001"
    BUSINESS_RULE = "AS-4002"
    ILLEGAL_TRANSITION = "AS-4003"
    IDEMPOTENCY_CONFLICT = "AS-4004"
    UNAUTHORIZED = "AS-4010"
    FORBIDDEN = "AS-4030"
    RATE_LIMITED = "AS-4290"
    INTERNAL = "AS-5001"
    NOT_IMPLEMENTED = "AS-5002"
    BREAKER_OPEN = "AS-5031"
    DEPENDENCY_UNAVAILABLE = "AS-5032"
    UPSTREAM_MODEL_UNAVAILABLE = "AS-5033"
    NOT_FOUND = "AS-4040"


@dataclass(frozen=True, slots=True)
class ErrorSpec:
    http_status: int
    message: str


ERROR_SPECS: Mapping[ErrorCode, ErrorSpec] = {
    ErrorCode.PARAM_INVALID: ErrorSpec(400, "参数校验失败"),
    ErrorCode.BUSINESS_RULE: ErrorSpec(422, "业务规则不允许"),
    ErrorCode.ILLEGAL_TRANSITION: ErrorSpec(409, "状态机非法迁移 / 版本冲突"),
    ErrorCode.IDEMPOTENCY_CONFLICT: ErrorSpec(409, "幂等冲突（同键不同参）"),
    ErrorCode.UNAUTHORIZED: ErrorSpec(401, "鉴权失败"),
    ErrorCode.FORBIDDEN: ErrorSpec(403, "权限不足（写操作分级）"),
    ErrorCode.NOT_FOUND: ErrorSpec(404, "资源不存在"),
    ErrorCode.RATE_LIMITED: ErrorSpec(429, "请求过于频繁，请稍后再试"),
    ErrorCode.INTERNAL: ErrorSpec(500, "内部错误"),
    ErrorCode.NOT_IMPLEMENTED: ErrorSpec(501, "该能力尚未实现"),
    ErrorCode.BREAKER_OPEN: ErrorSpec(503, "写链路熔断中，已转人工处理"),
    ErrorCode.DEPENDENCY_UNAVAILABLE: ErrorSpec(503, "依赖组件不可用"),
    ErrorCode.UPSTREAM_MODEL_UNAVAILABLE: ErrorSpec(503, "模型服务不可用，已降级转人工"),
}


def spec_of(code: ErrorCode) -> ErrorSpec:
    return ERROR_SPECS[code]


class AppError(Exception):
    """业务异常基类。所有可预期的失败都必须以它或子类抛出。"""

    def __init__(
        self,
        code: ErrorCode,
        message: str | None = None,
        *,
        detail: Mapping[str, Any] | None = None,
        http_status: int | None = None,
    ) -> None:
        spec = spec_of(code)
        self.code = code
        self.message = message or spec.message
        self.detail: dict[str, Any] = dict(detail or {})
        self.http_status = http_status or spec.http_status
        super().__init__(f"{code}: {self.message}")

    def to_payload(self, trace_id: str | None) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "code": str(self.code),
            "message": self.message,
            "detail": self.detail,
            "trace_id": trace_id,
        }
        return payload


class IllegalTransitionError(AppError):
    """状态机非法迁移（Spec §5.2 约束 3）。出现任意一次即 P1 事件。"""

    def __init__(
        self,
        *,
        biz_id: str,
        from_status: str,
        event: str,
        allowed: Sequence[str] = (),
        detail: Mapping[str, Any] | None = None,
    ) -> None:
        merged = {
            "biz_id": biz_id,
            "from_status": from_status,
            "event": event,
            "allowed": list(allowed),
        }
        merged.update(detail or {})
        super().__init__(
            ErrorCode.ILLEGAL_TRANSITION,
            f"非法状态迁移：{from_status} --{event}--> ?",
            detail=merged,
        )


class IdempotencyConflictError(AppError):
    """同幂等键不同参数（Spec §9.1 AS-4004）。"""

    def __init__(self, idem_key: str, detail: Mapping[str, Any] | None = None) -> None:
        merged: dict[str, Any] = {"idem_key": idem_key}
        merged.update(detail or {})
        super().__init__(
            ErrorCode.IDEMPOTENCY_CONFLICT,
            "幂等键已存在且参数不一致",
            detail=merged,
        )


class BreakerOpenError(AppError):
    def __init__(self, scope: str = "write") -> None:
        super().__init__(
            ErrorCode.BREAKER_OPEN,
            f"{scope} 链路熔断中，已转人工处理",
            detail={"scope": scope},
        )


@dataclass(slots=True)
class ErrorResponseBody:
    """OpenAPI 文档用的错误响应模型（与运行时 payload 一致）。"""

    code: str
    message: str
    detail: dict[str, Any] = field(default_factory=dict)
    trace_id: str | None = None


def not_implemented(feature: str, milestone: str = "M1") -> NoReturn:
    """统一"未实现"出口：返回 501 + AS-5002，避免用 404/裸 500 误导联调。"""
    raise AppError(
        ErrorCode.NOT_IMPLEMENTED,
        f"{feature} 将于 {milestone} 交付",
        detail={"milestone": milestone, "feature": feature},
    )
