"""售后状态机（Spec §5.2「唯一权威来源」）。

M0 落地**纯函数层**：状态/事件枚举、迁移表、校验函数。写库（乐观锁 + 流水 + outbox）
在 M2 以同一个 ``TRANSITIONS`` 为唯一依据实现，杜绝"表格与代码漂移"。

关键约束（对应 Spec §5.2）：

1. 迁移函数是**唯一**修改状态的入口；
2. 每次迁移携带 ``version`` 乐观锁，影响行数为 0 即冲突（M2 实现）；
3. 非表内迁移抛 ``IllegalTransitionError``（``AS-4003``）并计入 ``transition_illegal_total``。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Final

from app.api.errors import ErrorCode, IllegalTransitionError


class AfterSalesStatus(StrEnum):
    DRAFT = "DRAFT"
    PENDING_APPROVAL = "PENDING_APPROVAL"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    PROCESSING = "PROCESSING"
    DONE = "DONE"
    FAILED = "FAILED"
    CANCELED = "CANCELED"


class AfterSalesEvent(StrEnum):
    SUBMIT = "SUBMIT"
    ABANDON = "ABANDON"
    APPROVE = "APPROVE"
    REJECT = "REJECT"
    CANCEL = "CANCEL"
    EXECUTE = "EXECUTE"
    COMPLETE = "COMPLETE"
    FAIL = "FAIL"
    RETRY = "RETRY"
    ESCALATE = "ESCALATE"


TERMINAL_STATUSES: Final[frozenset[AfterSalesStatus]] = frozenset(
    {AfterSalesStatus.DONE, AfterSalesStatus.REJECTED, AfterSalesStatus.CANCELED}
)


@dataclass(frozen=True, slots=True)
class Transition:
    """一行迁移规则（与 Spec §5.2 表格逐行对应）。"""

    from_status: AfterSalesStatus
    event: AfterSalesEvent
    to_status: AfterSalesStatus | None
    """``None`` 表示终态删除（DRAFT --ABANDON--> 删除）/转人工工单（FAILED --ESCALATE-->）。"""
    actor: str
    guard: str = ""

    @property
    def key(self) -> tuple[AfterSalesStatus, AfterSalesEvent]:
        return (self.from_status, self.event)


#: Spec §5.2 状态迁移表 —— 唯一权威来源，禁止在别处硬编码状态流转。
TRANSITIONS: Final[tuple[Transition, ...]] = (
    Transition(
        AfterSalesStatus.DRAFT,
        AfterSalesEvent.SUBMIT,
        AfterSalesStatus.PENDING_APPROVAL,
        "agent/user",
        "参数校验通过 + 幂等键未占用",
    ),
    Transition(AfterSalesStatus.DRAFT, AfterSalesEvent.ABANDON, None, "user"),
    Transition(
        AfterSalesStatus.PENDING_APPROVAL,
        AfterSalesEvent.APPROVE,
        AfterSalesStatus.APPROVED,
        "approver",
        "状态版本号匹配",
    ),
    Transition(
        AfterSalesStatus.PENDING_APPROVAL,
        AfterSalesEvent.REJECT,
        AfterSalesStatus.REJECTED,
        "approver",
        "状态版本号匹配",
    ),
    Transition(
        AfterSalesStatus.PENDING_APPROVAL,
        AfterSalesEvent.CANCEL,
        AfterSalesStatus.CANCELED,
        "user/system",
        "无执行中任务",
    ),
    Transition(
        AfterSalesStatus.APPROVED,
        AfterSalesEvent.EXECUTE,
        AfterSalesStatus.PROCESSING,
        "worker",
        "工单创建成功",
    ),
    Transition(
        AfterSalesStatus.APPROVED, AfterSalesEvent.CANCEL, AfterSalesStatus.CANCELED, "user/agent"
    ),
    Transition(
        AfterSalesStatus.PROCESSING,
        AfterSalesEvent.COMPLETE,
        AfterSalesStatus.DONE,
        "worker/callback",
        "逆向物流签收/退款到账",
    ),
    Transition(
        AfterSalesStatus.PROCESSING,
        AfterSalesEvent.FAIL,
        AfterSalesStatus.FAILED,
        "worker",
        "错误码可重试",
    ),
    Transition(
        AfterSalesStatus.FAILED,
        AfterSalesEvent.RETRY,
        AfterSalesStatus.PROCESSING,
        "worker",
        "重试次数 < 3",
    ),
    Transition(AfterSalesStatus.FAILED, AfterSalesEvent.ESCALATE, None, "system/breaker"),
)

TRANSITION_MAP: Final[dict[tuple[AfterSalesStatus, AfterSalesEvent], Transition]] = {
    item.key: item for item in TRANSITIONS
}


def lookup_transition(current: AfterSalesStatus, event: AfterSalesEvent) -> Transition | None:
    return TRANSITION_MAP.get((current, event))


def allowed_events(current: AfterSalesStatus) -> tuple[AfterSalesEvent, ...]:
    """当前状态下合法事件集合（用于错误明细与前端话术）。"""
    return tuple(item.event for item in TRANSITIONS if item.from_status == current)


def is_terminal(status: AfterSalesStatus) -> bool:
    return status in TERMINAL_STATUSES


def validate_transition(
    *,
    biz_id: str,
    current: AfterSalesStatus,
    event: AfterSalesEvent,
) -> Transition:
    """校验迁移合法性；非法迁移抛 ``IllegalTransitionError``（AS-4003）。"""
    transition = lookup_transition(current, event)
    if transition is None:
        raise IllegalTransitionError(
            biz_id=biz_id,
            from_status=str(current),
            event=str(event),
            allowed=[str(item) for item in allowed_events(current)],
            detail={
                "code": str(ErrorCode.ILLEGAL_TRANSITION),
                "terminal": is_terminal(current),
                "legal_events": [str(item) for item in allowed_events(current)],
            },
        )
    return transition


def next_status(transition: Transition) -> AfterSalesStatus:
    """迁移后的状态；终态删除/转人工返回原状态是不可能的，用显式异常兜底。"""
    if transition.to_status is None:
        msg = f"{transition.from_status} --{transition.event}--> 不是状态跃迁（删除/转人工）"
        raise ValueError(msg)
    return transition.to_status


def apply_event(
    *,
    biz_id: str,
    current: AfterSalesStatus,
    event: AfterSalesEvent,
) -> AfterSalesStatus | None:
    """纯状态推进；返回 ``None`` 表示终态删除 / 转人工工单。"""
    transition = validate_transition(biz_id=biz_id, current=current, event=event)
    return transition.to_status
