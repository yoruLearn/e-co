"""状态机测试：Spec §5.2 迁移表全量守护。

这是 M0 最重要的一组测试——把"唯一权威来源"的表格钉死，M2 接 DB 时只需实现
乐观锁事务，不会出现代码与表格不一致（§23.2 第一条检查项）。
"""

from __future__ import annotations

import itertools

import pytest

from app.api.errors import AppError, IllegalTransitionError
from app.services.state_machine import (
    TRANSITIONS,
    AfterSalesEvent,
    AfterSalesStatus,
    allowed_events,
    apply_event,
    is_terminal,
    lookup_transition,
    next_status,
    validate_transition,
)

#: Spec §5.2 表格原文（11 行），逐行对照
SPEC_ROWS = [
    ("DRAFT", "SUBMIT", "PENDING_APPROVAL"),
    ("DRAFT", "ABANDON", None),
    ("PENDING_APPROVAL", "APPROVE", "APPROVED"),
    ("PENDING_APPROVAL", "REJECT", "REJECTED"),
    ("PENDING_APPROVAL", "CANCEL", "CANCELED"),
    ("APPROVED", "EXECUTE", "PROCESSING"),
    ("APPROVED", "CANCEL", "CANCELED"),
    ("PROCESSING", "COMPLETE", "DONE"),
    ("PROCESSING", "FAIL", "FAILED"),
    ("FAILED", "RETRY", "PROCESSING"),
    ("FAILED", "ESCALATE", None),
]


def test_transition_table_matches_spec_rows() -> None:
    actual = [
        (str(row.from_status), str(row.event), str(row.to_status) if row.to_status else None)
        for row in TRANSITIONS
    ]
    assert actual == SPEC_ROWS


def test_transition_keys_are_unique() -> None:
    keys = [row.key for row in TRANSITIONS]
    assert len(keys) == len(set(keys))


@pytest.mark.parametrize(
    ("from_status", "event", "to_status"),
    [(row[0], row[1], row[2]) for row in SPEC_ROWS],
)
def test_legal_transition_matches_table(
    from_status: str, event: str, to_status: str | None
) -> None:
    transition = validate_transition(
        biz_id="AS1",
        current=AfterSalesStatus(from_status),
        event=AfterSalesEvent(event),
    )
    assert (str(transition.to_status) if transition.to_status else None) == to_status


@pytest.mark.parametrize(
    ("from_status", "event"),
    [
        (status, event)
        for status, event in itertools.product(AfterSalesStatus, AfterSalesEvent)
        if (str(status), str(event)) not in {(row[0], row[1]) for row in SPEC_ROWS}
    ],
)
def test_illegal_transition_raises_with_error_code(from_status: str, event: str) -> None:
    with pytest.raises(IllegalTransitionError) as excinfo:
        validate_transition(
            biz_id="AS1",
            current=AfterSalesStatus(from_status),
            event=AfterSalesEvent(event),
        )
    error = excinfo.value
    assert isinstance(error, AppError)
    assert str(error.code) == "AS-4003"
    assert error.detail["biz_id"] == "AS1"
    assert error.detail["from_status"] == from_status
    assert error.detail["event"] == event


def test_terminal_states_have_no_transitions() -> None:
    for status in (AfterSalesStatus.DONE, AfterSalesStatus.REJECTED, AfterSalesStatus.CANCELED):
        assert is_terminal(status) is True
        assert allowed_events(status) == ()


def test_non_terminal_states_are_not_terminal() -> None:
    for status in (
        AfterSalesStatus.DRAFT,
        AfterSalesStatus.PENDING_APPROVAL,
        AfterSalesStatus.APPROVED,
        AfterSalesStatus.PROCESSING,
        AfterSalesStatus.FAILED,
    ):
        assert is_terminal(status) is False


def test_allowed_events_are_ordered_and_complete() -> None:
    assert allowed_events(AfterSalesStatus.PENDING_APPROVAL) == (
        AfterSalesEvent.APPROVE,
        AfterSalesEvent.REJECT,
        AfterSalesEvent.CANCEL,
    )
    assert allowed_events(AfterSalesStatus.FAILED) == (
        AfterSalesEvent.RETRY,
        AfterSalesEvent.ESCALATE,
    )


def test_apply_event_returns_next_status_or_none() -> None:
    assert (
        apply_event(biz_id="AS1", current=AfterSalesStatus.DRAFT, event=AfterSalesEvent.SUBMIT)
        == AfterSalesStatus.PENDING_APPROVAL
    )
    assert (
        apply_event(biz_id="AS1", current=AfterSalesStatus.DRAFT, event=AfterSalesEvent.ABANDON)
        is None
    )
    assert (
        apply_event(biz_id="AS1", current=AfterSalesStatus.FAILED, event=AfterSalesEvent.ESCALATE)
        is None
    )


def test_retry_loop_only_from_failed() -> None:
    assert (
        apply_event(biz_id="AS1", current=AfterSalesStatus.FAILED, event=AfterSalesEvent.RETRY)
        == AfterSalesStatus.PROCESSING
    )
    with pytest.raises(IllegalTransitionError):
        apply_event(biz_id="AS1", current=AfterSalesStatus.PROCESSING, event=AfterSalesEvent.RETRY)


def test_next_status_rejects_non_transition_rows() -> None:
    abandon = lookup_transition(AfterSalesStatus.DRAFT, AfterSalesEvent.ABANDON)
    assert abandon is not None
    with pytest.raises(ValueError, match="不是状态跃迁"):
        next_status(abandon)


def test_lookup_returns_none_for_unknown_pair() -> None:
    assert lookup_transition(AfterSalesStatus.DONE, AfterSalesEvent.SUBMIT) is None
