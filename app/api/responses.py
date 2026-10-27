"""统一响应信封（Spec §9.1）。

成功：``{"code": 0, "message": "ok", "data": {...}, "trace_id": "..."}``
失败：``{"code": "AS-xxxx", "message": "...", "detail": {...}, "trace_id": "..."}``

SSE（``/chat/stream``）与 ``/metrics`` 不属于信封范畴，单独在路由/挂载中处理。
"""

from __future__ import annotations

from typing import Any, Generic, TypeVar

from pydantic import BaseModel, Field

from app.common.trace import get_trace_id

DataT = TypeVar("DataT")


class SuccessEnvelope(BaseModel, Generic[DataT]):
    """OpenAPI 成功响应模型。"""

    code: int = 0
    message: str = "ok"
    data: DataT
    trace_id: str | None = None


class ErrorEnvelope(BaseModel):
    """OpenAPI 错误响应模型（与 ``AppError.to_payload`` 一致）。"""

    code: str
    message: str
    detail: dict[str, Any] = Field(default_factory=dict)
    trace_id: str | None = None


def ok(data: Any, trace_id: str | None = None) -> dict[str, Any]:
    """构造成功信封；trace_id 缺省取当前上下文。"""
    return {
        "code": 0,
        "message": "ok",
        "data": data,
        "trace_id": trace_id or get_trace_id(),
    }


def error_payload(
    code: str,
    message: str,
    detail: dict[str, Any] | None = None,
    trace_id: str | None = None,
) -> dict[str, Any]:
    return {
        "code": code,
        "message": message,
        "detail": detail or {},
        "trace_id": trace_id or get_trace_id(),
    }


ERROR_RESPONSES: dict[int | str, dict[str, Any]] = {
    400: {"model": ErrorEnvelope, "description": "AS-4001 参数校验失败"},
    401: {"model": ErrorEnvelope, "description": "AS-4010 鉴权失败"},
    403: {"model": ErrorEnvelope, "description": "AS-4030 权限不足"},
    404: {"model": ErrorEnvelope, "description": "AS-4040 资源不存在"},
    409: {"model": ErrorEnvelope, "description": "AS-4003/AS-4004 状态或幂等冲突"},
    422: {"model": ErrorEnvelope, "description": "AS-4002 业务规则不允许"},
    429: {"model": ErrorEnvelope, "description": "AS-4290 限流"},
    500: {"model": ErrorEnvelope, "description": "AS-5001 内部错误"},
    501: {"model": ErrorEnvelope, "description": "AS-5002 尚未实现（M1/M2）"},
    503: {"model": ErrorEnvelope, "description": "AS-5031/AS-5032 熔断或依赖不可用"},
}
