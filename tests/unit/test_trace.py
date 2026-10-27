"""trace 上下文测试（Spec §13.1）。"""

from __future__ import annotations

import re

from app.common.trace import (
    TRACE_ID_PATTERN,
    get_session_id,
    get_trace_id,
    is_valid_trace_id,
    new_trace_id,
    normalize_trace_id,
    reset_trace_context,
    set_session_id,
    trace_scope,
)


def test_generated_trace_id_matches_spec_charset_and_length() -> None:
    trace_id = new_trace_id()
    assert TRACE_ID_PATTERN.match(trace_id)
    assert 16 <= len(trace_id) <= 64
    assert trace_id.startswith("tr_")


def test_valid_header_is_passthrough() -> None:
    raw = "abc123_-ABC123_-xyz"
    assert is_valid_trace_id(raw) is True
    assert normalize_trace_id(raw) == raw


def test_short_or_illegal_header_is_replaced() -> None:
    for raw in (None, "", "short", "bad trace with spaces", "危险字符" * 3, "a" * 65):
        assert is_valid_trace_id(raw) is False
        normalized = normalize_trace_id(raw)
        assert normalized != raw
        assert re.fullmatch(r"tr_[0-9a-f]{32}", normalized)


def test_trace_scope_sets_and_restores_context() -> None:
    reset_trace_context()
    assert get_trace_id() is None
    with trace_scope("tr_scope0123456789ab", session_id="sess_1") as trace_id:
        assert trace_id == "tr_scope0123456789ab"
        assert get_trace_id() == trace_id
        assert get_session_id() == "sess_1"
    assert get_trace_id() is None
    assert get_session_id() is None


def test_trace_scope_generates_id_when_missing() -> None:
    with trace_scope() as trace_id:
        assert TRACE_ID_PATTERN.match(trace_id)


def test_session_id_can_be_set_directly() -> None:
    set_session_id("sess_x")
    assert get_session_id() == "sess_x"
    set_session_id(None)
    assert get_session_id() is None
