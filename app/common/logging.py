"""结构化日志（Spec §13.1 / §16.2「禁止 print；出入参脱敏后记录」）。

输出形如::

    {"ts":"2026-10-27T09:12:00.123Z","level":"INFO","logger":"app.api.access",
     "msg":"http.request","trace_id":"tr_8f3c...","method":"GET","path":"/healthz",
     "status":200,"ms":3}

设计取舍：

- 不引入 structlog：仅需 JSON Formatter + contextvars，减少依赖面；
- 附加字段走 ``extra=`` 或 ``log_event(**fields)``，不在 message 里拼字符串；
- 命名敏感键（password/token/authorization/api_key/secret/confirm_token）自动打码，
  避免凭证经日志外泄（§14 脱敏）。
"""

from __future__ import annotations

import json
import logging
import sys
from contextvars import ContextVar
from datetime import UTC, datetime
from typing import Any, Final

from app.common.trace import get_session_id, get_trace_id

_CONTEXT: ContextVar[dict[str, Any] | None] = ContextVar("log_context", default=None)

_LOG_RECORD_RESERVED: Final[frozenset[str]] = frozenset(
    {
        "args",
        "asctime",
        "created",
        "exc_info",
        "exc_text",
        "filename",
        "funcName",
        "levelname",
        "levelno",
        "lineno",
        "module",
        "msecs",
        "message",
        "msg",
        "name",
        "pathname",
        "process",
        "processName",
        "relativeCreated",
        "stack_info",
        "thread",
        "threadName",
        "taskName",
    }
)

_SENSITIVE_KEYS: Final[frozenset[str]] = frozenset(
    {
        "password",
        "passwd",
        "token",
        "access_token",
        "refresh_token",
        "authorization",
        "api_key",
        "apikey",
        "secret",
        "jwt",
        "confirm_token",
        "idempotency_key",
    }
)

_MASK: Final[str] = "***"


def mask_secret(value: str) -> str:
    """保留可读性同时避免泄露：``sk-abcd...`` → ``sk-a***``。"""
    if not value:
        return _MASK
    if len(value) <= 6:
        return _MASK
    return f"{value[:4]}{_MASK}"


def redact(payload: dict[str, Any]) -> dict[str, Any]:
    """递归打码敏感键（大小写不敏感，支持 ``Authorization`` 等写法）。"""
    cleaned: dict[str, Any] = {}
    for key, value in payload.items():
        lowered = key.lower()
        if lowered in _SENSITIVE_KEYS or lowered.endswith("_token"):
            cleaned[key] = _MASK
        elif isinstance(value, dict):
            cleaned[key] = redact(value)
        elif isinstance(value, list):
            cleaned[key] = [redact(item) if isinstance(item, dict) else item for item in value]
        else:
            cleaned[key] = value
    return cleaned


def _iso_timestamp(created: float) -> str:
    dt = datetime.fromtimestamp(created, tz=UTC)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


class JsonFormatter(logging.Formatter):
    """JSON line 格式器：固定字段 + contextvars + extra。"""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": _iso_timestamp(record.created),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        trace_id = getattr(record, "trace_id", None) or get_trace_id()
        if trace_id:
            payload["trace_id"] = trace_id
        session_id = getattr(record, "session_id", None) or get_session_id()
        if session_id:
            payload["session_id"] = session_id

        payload.update(_CONTEXT.get() or {})

        extras = {
            key: value
            for key, value in record.__dict__.items()
            if key not in _LOG_RECORD_RESERVED
            and key not in {"trace_id", "session_id"}
            and not key.startswith("_")
        }
        payload.update(redact(extras))

        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        if record.stack_info:
            payload["stack"] = self.formatStack(record.stack_info)

        return json.dumps(payload, ensure_ascii=False, default=str)


class ConsoleFormatter(logging.Formatter):
    """本地排障用单行文本格式（仍带 trace_id）。"""

    def format(self, record: logging.LogRecord) -> str:
        trace_id = getattr(record, "trace_id", None) or get_trace_id() or "-"
        base = (
            f"{_iso_timestamp(record.created)} {record.levelname:<7} "
            f"[{trace_id}] {record.name}: {record.getMessage()}"
        )
        extras = {
            key: value
            for key, value in record.__dict__.items()
            if key not in _LOG_RECORD_RESERVED
            and key not in {"trace_id", "session_id"}
            and not key.startswith("_")
        }
        if extras:
            base += " " + json.dumps(redact(extras), ensure_ascii=False, default=str)
        if record.exc_info:
            base += "\n" + self.formatException(record.exc_info)
        return base


def configure_logging(level: str = "INFO", fmt: str = "json", *, force: bool = False) -> None:
    """装配 root logger；可重复调用（幂等），测试用 ``force=True`` 重装。"""
    handler = logging.StreamHandler(stream=sys.stdout)
    handler.setFormatter(JsonFormatter() if fmt == "json" else ConsoleFormatter())

    root = logging.getLogger()
    if force:
        for existing in list(root.handlers):
            root.removeHandler(existing)
            existing.close()
    if not root.handlers:
        root.addHandler(handler)
    root.setLevel(level)

    # uvicorn 自带 handler 会造成重复输出，统一交给 root
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        logger = logging.getLogger(name)
        logger.handlers.clear()
        logger.propagate = True

    # httpx 默认 INFO 会为每个请求打一行（含 URL），对生产是噪音
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


def update_log_context(**fields: Any) -> None:
    """合并进程/请求上下文（如 ``env``、``node``），后续日志自动携带。"""
    current = dict(_CONTEXT.get() or {})
    current.update(fields)
    _CONTEXT.set(current)


def clear_log_context() -> None:
    _CONTEXT.set({})


def get_logger(name: str) -> logging.Logger:
    """统一获取 logger（便于未来挂全局处理器/采样，不在业务里直接 getLogger）。"""
    return logging.getLogger(name)


def sanitize_extra(fields: dict[str, Any]) -> dict[str, Any]:
    """避免 ``extra`` 覆盖 LogRecord 保留字段。

    ``logger.info(..., extra={"message": ...})`` 会抛 ``KeyError: Attempt to overwrite
    'message' in LogRecord``——错误处理器里再抛异常会把 4xx/5xx 变成 500，
    所以统一加前缀转义，保证"记日志"永远不会打断"回响应"。
    """
    return {
        (f"extra_{key}" if key in _LOG_RECORD_RESERVED else key): value
        for key, value in fields.items()
    }


def log_event(logger: logging.Logger, event: str, /, **fields: Any) -> None:
    """结构化事件：``log_event(log, "rag.hit", top1=0.82, cache=False)``。"""
    logger.info(event, extra=sanitize_extra(fields))


def log_warning(logger: logging.Logger, event: str, /, **fields: Any) -> None:
    logger.warning(event, extra=sanitize_extra(fields))


def log_error(logger: logging.Logger, event: str, /, **fields: Any) -> None:
    logger.error(event, extra=sanitize_extra(fields))
