"""统一异常处理器（Spec §9.1）。

所有出口（含 404/422/未捕获异常）都必须返回统一错误信封并带 ``trace_id``，
否则前端无法按 code 做话术映射（§22 错误码-话术映射表）。
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import cast

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.responses import Response

from app.api.errors import AppError, ErrorCode, spec_of
from app.api.responses import error_payload
from app.common.logging import log_error, log_event
from app.common.trace import get_trace_id

logger = logging.getLogger("app.api.errors")

_HTTP_STATUS_CODE_MAP: dict[int, ErrorCode] = {
    400: ErrorCode.PARAM_INVALID,
    401: ErrorCode.UNAUTHORIZED,
    403: ErrorCode.FORBIDDEN,
    404: ErrorCode.NOT_FOUND,
    405: ErrorCode.PARAM_INVALID,
    422: ErrorCode.BUSINESS_RULE,
    429: ErrorCode.RATE_LIMITED,
    501: ErrorCode.NOT_IMPLEMENTED,
}

_MAX_VALIDATION_ERRORS = 20


async def handle_app_error(_request: Request, exc: AppError) -> JSONResponse:
    if exc.http_status >= 500:
        log_error(
            logger,
            "app.error",
            code=str(exc.code),
            reason=exc.message,
            detail=exc.detail,
        )
    else:
        log_event(logger, "app.rejected", code=str(exc.code), reason=exc.message)
    return JSONResponse(status_code=exc.http_status, content=exc.to_payload(get_trace_id()))


async def handle_validation_error(_request: Request, exc: RequestValidationError) -> JSONResponse:
    details = [
        {
            "loc": ".".join(str(part) for part in error.get("loc", ())),
            "msg": error.get("msg", ""),
            "type": error.get("type", ""),
        }
        for error in exc.errors()[:_MAX_VALIDATION_ERRORS]
    ]
    log_event(logger, "request.invalid", field_errors=len(details))
    return JSONResponse(
        status_code=400,
        content=error_payload(
            str(ErrorCode.PARAM_INVALID),
            spec_of(ErrorCode.PARAM_INVALID).message,
            {"errors": details},
        ),
    )


async def handle_http_exception(_request: Request, exc: StarletteHTTPException) -> Response:
    if exc.status_code in _HTTP_STATUS_CODE_MAP:
        code = _HTTP_STATUS_CODE_MAP[exc.status_code]
    elif exc.status_code >= 500:
        code = ErrorCode.INTERNAL
    else:
        code = ErrorCode.BUSINESS_RULE
    detail = exc.detail
    message = detail if isinstance(detail, str) and detail else spec_of(code).message
    if exc.status_code >= 500:
        log_error(logger, "http.error", status=exc.status_code, detail=str(detail)[:300])
    return JSONResponse(
        status_code=exc.status_code,
        content=error_payload(
            str(code),
            message,
            {} if isinstance(detail, str) else {"detail": detail},
        ),
        headers=getattr(exc, "headers", None),
    )


async def handle_unexpected(_request: Request, exc: Exception) -> JSONResponse:
    logger.exception("unhandled.exception", extra={"error_type": type(exc).__name__})
    return JSONResponse(
        status_code=500,
        content=error_payload(
            str(ErrorCode.INTERNAL),
            spec_of(ErrorCode.INTERNAL).message,
            {"hint": "请将该 trace_id 提供给值班同学排查"},
        ),
    )


def register_exception_handlers(app: FastAPI) -> None:
    handler_type = Callable[[Request, Exception], Awaitable[Response]]
    app.add_exception_handler(AppError, cast(handler_type, handle_app_error))
    app.add_exception_handler(RequestValidationError, cast(handler_type, handle_validation_error))
    app.add_exception_handler(StarletteHTTPException, cast(handler_type, handle_http_exception))
    app.add_exception_handler(Exception, cast(handler_type, handle_unexpected))
