"""trace_id 上下文传播（Spec §13.1）。

- trace_id 由网关生成或透传 ``X-Trace-Id``；长度 16–64，字符集 ``[A-Za-z0-9_-]``；
- 传播路径：HTTP 头 → ``contextvars`` → 结构化日志 → （M1 起）LangGraph state → MQ 信封 → MySQL；
- 非法或缺失时生成新 ID 并记一条告警级日志，绝不因为 header 脏而 4xx/5xx。
"""

from __future__ import annotations

import contextlib
import re
import secrets
from collections.abc import Iterator
from contextvars import ContextVar
from typing import Final

TRACE_ID_PATTERN: Final[re.Pattern[str]] = re.compile(r"^[A-Za-z0-9_-]{16,64}$")
TRACE_HEADER: Final[str] = "X-Trace-Id"
SESSION_HEADER: Final[str] = "X-Session-Id"

_trace_id_var: ContextVar[str | None] = ContextVar("trace_id", default=None)
_session_id_var: ContextVar[str | None] = ContextVar("session_id", default=None)


def new_trace_id() -> str:
    """生成 34 字符 trace_id（``tr_`` + 32 hex），满足 16–64 长度约束。"""
    return f"tr_{secrets.token_hex(16)}"


def is_valid_trace_id(value: str | None) -> bool:
    return bool(value) and TRACE_ID_PATTERN.match(value or "") is not None


def normalize_trace_id(raw: str | None) -> str:
    """透传合法 trace_id，否则生成新的（防日志注入、防超长 header）。"""
    if raw is not None:
        candidate = raw.strip()
        if is_valid_trace_id(candidate):
            return candidate
    return new_trace_id()


def get_trace_id() -> str | None:
    return _trace_id_var.get()


def set_trace_id(value: str) -> None:
    _trace_id_var.set(value)


def reset_trace_id(token: object) -> None:
    """接受 ``ContextVar.set`` 返回的 token；类型放宽以便中间件无需泛型参数。"""
    _trace_id_var.reset(token)  # type: ignore[arg-type]


def get_session_id() -> str | None:
    return _session_id_var.get()


def set_session_id(value: str | None) -> None:
    _session_id_var.set(value)


def reset_trace_context() -> None:
    """清空当前上下文（测试隔离 / worker 任务边界）。"""
    _trace_id_var.set(None)
    _session_id_var.set(None)


@contextlib.contextmanager
def trace_scope(trace_id: str | None = None, session_id: str | None = None) -> Iterator[str]:
    """在 worker / 后台任务中建立 trace 上下文。"""
    resolved = normalize_trace_id(trace_id) if trace_id else new_trace_id()
    token = _trace_id_var.set(resolved)
    session_token = _session_id_var.set(session_id) if session_id else None
    try:
        yield resolved
    finally:
        _trace_id_var.reset(token)
        if session_token is not None:
            _session_id_var.reset(session_token)
