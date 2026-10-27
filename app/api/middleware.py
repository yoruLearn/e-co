"""ASGI 中间件：trace 传播 + 访问日志 + RED 指标（Spec §13.1 / §13.2）。

为什么不使用 ``BaseHTTPMiddleware``：

- ``BaseHTTPMiddleware`` 会为每个请求额外起 task，SSE 长连接的取消/背压行为不直观；
- 纯 ASGI 中间件可以直接在 ``http.response.start`` 注入响应头，并与流式响应共用同一
  contextvars 上下文（M1 的 SSE 必须依赖这一点）。

两者都实现为纯 ASGI，因此无需 ``request.state`` 也能工作。
"""

from __future__ import annotations

import logging
import time
from typing import Any

from starlette.datastructures import Headers, MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.common.metrics import HTTP_LATENCY, HTTP_REQUESTS
from app.common.trace import (
    SESSION_HEADER,
    TRACE_HEADER,
    is_valid_trace_id,
    normalize_trace_id,
    set_session_id,
    set_trace_id,
)

access_logger = logging.getLogger("app.api.access")
security_logger = logging.getLogger("app.api.security")


def _route_label(scope: Scope) -> str:
    """低基数 path 标签：优先路由模板（``/api/v1/aftersales/{biz_id}``）。"""
    route: Any = scope.get("route")
    path = getattr(route, "path", None)
    if isinstance(path, str) and path:
        return path
    raw_path = scope.get("path")
    return raw_path if isinstance(raw_path, str) else "unmatched"


class TraceMiddleware:
    """透传/生成 trace_id，回写响应头，输出访问日志。"""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        headers = Headers(scope=scope)
        raw_trace = headers.get(TRACE_HEADER)
        trace_id = normalize_trace_id(raw_trace)
        if raw_trace is not None and not is_valid_trace_id(raw_trace):
            security_logger.warning(
                "trace.header.invalid",
                extra={"raw_header_len": len(raw_trace[:128])},
            )

        raw_session = headers.get(SESSION_HEADER)
        set_trace_id(trace_id)
        if raw_session:
            set_session_id(raw_session[:128])

        status_code = 500
        started = time.perf_counter()

        async def send_with_trace(message: Message) -> None:
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = int(message["status"])
                MutableHeaders(scope=message)[TRACE_HEADER] = trace_id
            await send(message)

        try:
            await self.app(scope, receive, send_with_trace)
        except Exception:
            access_logger.exception(
                "http.request.failed",
                extra={
                    "method": scope.get("method"),
                    "path": scope.get("path"),
                    "status": 500,
                },
            )
            raise
        finally:
            elapsed_ms = round((time.perf_counter() - started) * 1000, 2)
            access_logger.info(
                "http.request",
                extra={
                    "method": scope.get("method"),
                    "path": _route_label(scope),
                    "status": status_code,
                    "ms": elapsed_ms,
                    "client": (scope.get("client") or ("",))[0] or None,
                },
            )


class MetricsMiddleware:
    """记录 ``http_requests_total`` 与 ``http_request_duration_seconds``。"""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        started = time.perf_counter()
        status_code = 500

        async def send_with_metrics(message: Message) -> None:
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = int(message["status"])
            await send(message)

        try:
            await self.app(scope, receive, send_with_metrics)
        finally:
            path = _route_label(scope)
            method = scope.get("method", "GET")
            elapsed = time.perf_counter() - started
            HTTP_REQUESTS.labels(method=method, path=path, status=str(status_code)).inc()
            HTTP_LATENCY.labels(method=method, path=path).observe(elapsed)
