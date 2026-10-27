"""结构化日志与脱敏测试。"""

from __future__ import annotations

import json
import logging

from app.common.logging import (
    ConsoleFormatter,
    JsonFormatter,
    clear_log_context,
    configure_logging,
    log_error,
    log_event,
    mask_secret,
    redact,
    sanitize_extra,
    update_log_context,
)
from app.common.trace import set_trace_id


def _record(msg: str = "hello", **extra: object) -> logging.LogRecord:
    record = logging.LogRecord(
        name="app.test",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg=msg,
        args=(),
        exc_info=None,
    )
    for key, value in extra.items():
        setattr(record, key, value)
    return record


def test_json_formatter_emits_parseable_line_with_trace_id() -> None:
    set_trace_id("tr_0123456789abcdef")
    payload = json.loads(JsonFormatter().format(_record("http.request", status=200, ms=1.5)))
    assert payload["level"] == "INFO"
    assert payload["logger"] == "app.test"
    assert payload["msg"] == "http.request"
    assert payload["trace_id"] == "tr_0123456789abcdef"
    assert payload["status"] == 200
    assert payload["ms"] == 1.5
    assert payload["ts"].endswith("Z")


def test_json_formatter_includes_context_fields() -> None:
    update_log_context(env="test", release="dev")
    try:
        payload = json.loads(JsonFormatter().format(_record()))
        assert payload["env"] == "test"
        assert payload["release"] == "dev"
    finally:
        clear_log_context()


def test_sensitive_fields_are_redacted() -> None:
    cleaned = redact(
        {
            "password": "p@ss",
            "Authorization": "Bearer abc",
            "api_key": "sk-1234567890",
            "confirm_token": "ct_1",
            "nested": {"token": "abc"},
            "safe": "值",
        }
    )
    assert cleaned["password"] == "***"
    assert cleaned["Authorization"] == "***"
    assert cleaned["api_key"] == "***"
    assert cleaned["confirm_token"] == "***"
    assert cleaned["nested"] == {"token": "***"}
    assert cleaned["safe"] == "值"


def test_json_formatter_redacts_extra_fields() -> None:
    payload = json.loads(JsonFormatter().format(_record(password="secret-value")))
    assert payload["password"] == "***"
    assert "secret-value" not in json.dumps(payload, ensure_ascii=False)


def test_mask_secret_keeps_short_values_opaque() -> None:
    assert mask_secret("") == "***"
    assert mask_secret("abc") == "***"
    assert mask_secret("sk-abcdefgh") == "sk-a***"


def test_console_formatter_is_single_line() -> None:
    set_trace_id("tr_0123456789abcdef")
    line = ConsoleFormatter().format(_record("worker.tick", beats=3))
    assert line.count("\n") == 0
    assert "worker.tick" in line
    assert "beats" in line and "3" in line


def test_configure_logging_is_idempotent() -> None:
    configure_logging("INFO", "json")
    first = len(logging.getLogger().handlers)
    configure_logging("INFO", "json")
    assert len(logging.getLogger().handlers) == first
    assert logging.getLogger("uvicorn.access").propagate is True


def test_sanitize_extra_renames_logrecord_reserved_keys() -> None:
    cleaned = sanitize_extra({"message": "上游 500", "msg": "x", "code": "AS-5001"})
    assert cleaned == {"extra_message": "上游 500", "extra_msg": "x", "code": "AS-5001"}


def test_logging_with_reserved_key_does_not_raise() -> None:
    """错误处理器若用 ``extra={"message": ...}`` 会抛 KeyError 把 500 变 502。"""
    logger = logging.getLogger("app.test.reserved")
    logger.setLevel(logging.INFO)
    logger.propagate = False
    records: list[logging.LogRecord] = []

    class _Capture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

    handler = _Capture()
    logger.addHandler(handler)
    try:
        log_error(logger, "app.error", code="AS-5001", message="boom")
        log_event(logger, "app.rejected", reason="bad request")
    finally:
        logger.removeHandler(handler)

    assert len(records) == 2
    assert records[0].extra_message == "boom"
    assert records[1].reason == "bad request"
