"""Outbox 信封与重试策略测试（Spec §12.2 / §12.3）。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.services.outbox import (
    BASE_RETRY_SECONDS,
    MAX_RETRY,
    MAX_RETRY_SECONDS,
    MessageEnvelope,
    MessageSchema,
    OutboxStatus,
    Topic,
    build_envelope,
    new_msg_id,
    next_retry_at,
    retry_delay_seconds,
    should_dead_letter,
    utc_now_iso,
    validate_envelope,
)


def test_envelope_contains_all_spec_fields() -> None:
    envelope = build_envelope(
        biz_id="AS20261027001",
        trace_id="tr_0123456789abcdef",
        schema=MessageSchema.TICKET_CREATE_V1,
        payload={"action": "refund", "order_id": "O1", "amount": 199.0},
    )
    raw = envelope.as_dict()
    assert set(raw) == {"msg_id", "biz_id", "trace_id", "occurred_at", "schema", "payload"}
    assert raw["schema"] == "as.ticket.create.v1"
    assert raw["msg_id"].startswith("msg_")
    assert raw["occurred_at"].endswith("Z")
    assert envelope.sharding_key == "AS20261027001"


def test_envelope_roundtrip_and_hash_stability() -> None:
    envelope = build_envelope(
        biz_id="AS1",
        trace_id="tr_0123456789abcdef",
        schema=MessageSchema.TICKET_CREATE_V1,
        payload={"b": 2, "a": 1},
        msg_id="msg_fixed",
    )
    restored = validate_envelope(envelope.as_dict())
    assert restored.msg_id == "msg_fixed"
    assert restored.payload == {"a": 1, "b": 2}
    assert restored.payload_hash == envelope.payload_hash


def test_envelope_json_is_key_sorted() -> None:
    envelope = MessageEnvelope(
        msg_id="msg_1",
        biz_id="AS1",
        trace_id="tr_0123456789abcdef",
        occurred_at="2026-10-27T09:12:00Z",
        schema="as.ticket.create.v1",
        payload={"z": 1, "a": 2},
    )
    assert envelope.to_json().index('"a"') < envelope.to_json().index('"z"')


def test_validate_envelope_rejects_missing_fields() -> None:
    with pytest.raises(ValueError, match="缺少字段"):
        validate_envelope({"msg_id": "m", "biz_id": "b"})


def test_validate_envelope_rejects_non_object_payload() -> None:
    raw = build_envelope(
        biz_id="AS1",
        trace_id="tr_0123456789abcdef",
        schema=MessageSchema.TICKET_CREATE_V1,
        payload={"a": 1},
    ).as_dict()
    raw["payload"] = ["not", "an", "object"]
    with pytest.raises(TypeError, match="payload"):
        validate_envelope(raw)


def test_retry_backoff_sequence() -> None:
    assert [retry_delay_seconds(i) for i in range(4)] == [5, 15, 45, 135]
    assert retry_delay_seconds(10) == MAX_RETRY_SECONDS


def test_retry_rejects_negative() -> None:
    with pytest.raises(ValueError, match="retry"):
        retry_delay_seconds(-1)


def test_dead_letter_after_max_retry() -> None:
    assert should_dead_letter(MAX_RETRY) is False
    assert should_dead_letter(MAX_RETRY + 1) is True


def test_next_retry_at_uses_backoff() -> None:
    now = datetime(2026, 10, 27, 9, 0, tzinfo=UTC)
    assert next_retry_at(0, now) == now + timedelta(seconds=BASE_RETRY_SECONDS)
    assert next_retry_at(1, now) == now + timedelta(seconds=15)


def test_utc_now_iso_formats_naive_datetimes() -> None:
    assert utc_now_iso(datetime(2026, 10, 27, 9, 12)) == "2026-10-27T09:12:00Z"


def test_msg_id_is_unique() -> None:
    assert len({new_msg_id() for _ in range(50)}) == 50


def test_topic_and_status_enums_match_spec() -> None:
    assert {topic.value for topic in Topic} == {
        "AS_TICKET_CREATE",
        "AS_APPROVAL_EVENT",
        "AS_NOTIFY",
        "AS_EVAL_REPORT",
        "AS_TRACE_LOG",
        "AS_DEAD_LETTER",
    }
    assert {status.value for status in OutboxStatus} == {
        "pending",
        "published",
        "consumed",
        "dead",
    }
