"""幂等键与审批规则测试（Spec §9.1 / §11.2 / §3.3）。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from app.api.errors import AppError, IdempotencyConflictError
from app.common.config import Settings
from app.services.approval import (
    ApprovalDecision,
    ApprovalTaskStatus,
    approval_expiry,
    is_expired,
    needs_manual_approval,
    new_approval_task_id,
    plan_approval,
)
from app.services.idempotency import (
    IDEM_KEY_MAX_LEN,
    IdempotencyDecision,
    IdempotencyRecord,
    build_idem_key,
    canonical_payload,
    decide,
    ensure_idempotent,
    params_hash,
    parse_idem_key,
)


def test_build_and_parse_idem_key() -> None:
    key = build_idem_key("AS20261027001", "create")
    assert key == "AS20261027001:create"
    assert parse_idem_key(key) == ("AS20261027001", "create")


def test_idem_key_with_colons_in_biz_id_parses_right_part() -> None:
    assert parse_idem_key("AS1:sub:create") == ("AS1:sub", "create")


def test_idem_key_length_guard() -> None:
    with pytest.raises(ValueError, match=str(IDEM_KEY_MAX_LEN)):
        build_idem_key("A" * 90, "create")


def test_parse_idem_key_requires_separator() -> None:
    with pytest.raises(ValueError, match="格式"):
        parse_idem_key("AS1create")


def test_params_hash_is_order_insensitive() -> None:
    assert params_hash({"a": 1, "b": 2}) == params_hash({"b": 2, "a": 1})
    assert params_hash({"a": 1}) != params_hash({"a": 2})
    assert canonical_payload({"a": 1, "b": 2}) == '{"a":1,"b":2}'


def test_decide_new_replay_conflict() -> None:
    payload = {"amount": 199.0, "action": "refund"}
    record = IdempotencyRecord(params_hash=params_hash(payload), response_ref="biz:AS1")
    assert decide(None, payload) is IdempotencyDecision.NEW
    assert decide(record, payload) is IdempotencyDecision.REPLAY
    assert decide(record, {"amount": 1.0}) is IdempotencyDecision.CONFLICT


def test_ensure_idempotent_raises_as_4004() -> None:
    record = IdempotencyRecord(params_hash="deadbeef", response_ref="biz:AS1")
    with pytest.raises(IdempotencyConflictError) as excinfo:
        ensure_idempotent(record, {"amount": 1.0}, idem_key="AS1:create")
    error = excinfo.value
    assert isinstance(error, AppError)
    assert str(error.code) == "AS-4004"
    assert error.detail["incoming_params_hash"] == params_hash({"amount": 1.0})


def test_ensure_idempotent_passes_through() -> None:
    payload = {"amount": 1.0}
    record = IdempotencyRecord(params_hash=params_hash(payload), response_ref="biz:AS1")
    assert ensure_idempotent(record, payload, idem_key="AS1:create") is IdempotencyDecision.REPLAY


@pytest.mark.parametrize(
    ("amount", "expected"),
    [
        (None, False),
        (0, False),
        (499.99, False),
        (500, False),
        (500.01, True),
        (Decimal("9999.00"), True),
    ],
)
def test_approval_threshold_is_strictly_greater(
    amount: float | Decimal | None, expected: bool
) -> None:
    assert needs_manual_approval(amount, 500) is expected


def test_plan_approval_uses_settings() -> None:
    settings = Settings(_env_file=None, approval_threshold_cny=100)
    plan = plan_approval(150, settings)
    assert plan.required is True
    assert plan.reason == "amount_exceeds_threshold"
    assert plan.as_dict()["threshold_cny"] == 100.0

    auto = plan_approval(50, settings)
    assert auto.required is False
    assert auto.reason == "auto_approved"


def test_approval_task_id_format() -> None:
    task_id = new_approval_task_id()
    assert task_id.startswith("ap_")
    assert len(task_id) < 32


def test_approval_expiry_and_expired_check() -> None:
    settings = Settings(_env_file=None, idle_approval_expire_hours=2)
    now = datetime(2026, 10, 27, 9, 0, tzinfo=UTC)
    expires = approval_expiry(settings, now)
    assert expires == now + timedelta(hours=2)
    assert is_expired(expires, now) is False
    assert is_expired(expires, now + timedelta(hours=3)) is True
    # naive datetime 视为 UTC
    assert is_expired(expires.replace(tzinfo=None), now + timedelta(hours=3)) is True


def test_approval_enums_match_spec() -> None:
    assert {item.value for item in ApprovalDecision} == {"approve", "reject"}
    assert {item.value for item in ApprovalTaskStatus} == {
        "pending",
        "approved",
        "rejected",
        "expired",
    }
