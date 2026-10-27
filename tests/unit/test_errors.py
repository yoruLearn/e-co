"""错误码与异常测试：与 Spec §9.1 表格逐行对齐。"""

from __future__ import annotations

import pytest

from app.api.errors import (
    ERROR_SPECS,
    AppError,
    ErrorCode,
    IdempotencyConflictError,
    IllegalTransitionError,
    not_implemented,
    spec_of,
)

#: Spec §9.1 业务错误码 → HTTP 状态
SPEC_TABLE = {
    "AS-4001": 400,
    "AS-4002": 422,
    "AS-4003": 409,
    "AS-4004": 409,
    "AS-4010": 401,
    "AS-4030": 403,
    "AS-4290": 429,
    "AS-5001": 500,
    "AS-5031": 503,
}


def test_spec_error_codes_have_expected_http_status() -> None:
    for code, expected in SPEC_TABLE.items():
        spec = ERROR_SPECS[ErrorCode(code)]
        assert spec.http_status == expected, f"{code} 映射错误"
        assert spec.message


def test_every_error_code_has_spec_entry() -> None:
    assert set(ErrorCode) == set(ERROR_SPECS)


def test_app_error_payload_shape() -> None:
    error = AppError(ErrorCode.BUSINESS_RULE, "订单不可售后", detail={"order_id": "O1"})
    payload = error.to_payload("tr_x")
    assert payload == {
        "code": "AS-4002",
        "message": "订单不可售后",
        "detail": {"order_id": "O1"},
        "trace_id": "tr_x",
    }


def test_app_error_defaults_come_from_spec() -> None:
    error = AppError(ErrorCode.RATE_LIMITED)
    assert error.message == spec_of(ErrorCode.RATE_LIMITED).message
    assert error.http_status == 429


def test_illegal_transition_error_carries_diagnostics() -> None:
    error = IllegalTransitionError(
        biz_id="AS1", from_status="DONE", event="CANCEL", allowed=["NONE"]
    )
    payload = error.to_payload("tr_x")
    assert payload["code"] == "AS-4003"
    assert payload["detail"]["biz_id"] == "AS1"
    assert payload["detail"]["from_status"] == "DONE"
    assert payload["detail"]["allowed"] == ["NONE"]


def test_idempotency_conflict_error() -> None:
    error = IdempotencyConflictError("AS1:create", detail={"existing_params_hash": "h1"})
    assert str(error.code) == "AS-4004"
    assert error.detail["idem_key"] == "AS1:create"


def test_not_implemented_returns_501_with_milestone() -> None:
    with pytest.raises(AppError) as excinfo:
        not_implemented("对话链路", "M1")
    assert excinfo.value.http_status == 501
    assert excinfo.value.detail["milestone"] == "M1"
    assert str(excinfo.value.code) == "AS-5002"
