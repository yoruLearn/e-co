"""API 契约模型（Spec §9.2）。

M0 把请求/响应形状先固定下来：前端、客服工作台、Provider 三方可以并行联调，
业务实现按里程碑填入，不会因字段改名反复返工。
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, Field

from app.common.security import Role
from app.eval.cases import EvalCategory


# --------------------------------------------------------------------------- #
# SSE / 对话
# --------------------------------------------------------------------------- #
class ChatContext(BaseModel):
    channel: Literal["app", "web", "workbench"] = "app"
    entry: str | None = Field(default=None, max_length=64)


class ChatRequest(BaseModel):
    session_id: str = Field(min_length=1, max_length=128, description="会话 ID")
    message: str = Field(min_length=1, max_length=4000, description="用户本轮输入")
    context: ChatContext = Field(default_factory=ChatContext)
    confirm_token: str | None = Field(
        default=None, max_length=128, description="写操作双确认令牌（由 confirm 事件签发）"
    )


class Usage(BaseModel):
    tokens: int = 0
    llm_calls: int = 0
    cache_hits: int = 0


# --------------------------------------------------------------------------- #
# 探针
# --------------------------------------------------------------------------- #
class HealthData(BaseModel):
    status: Literal["ok"] = "ok"
    version: str
    env: str
    uptime_s: float


class CheckItem(BaseModel):
    name: str
    status: Literal["ok", "fail", "skipped"]
    detail: str = ""
    latency_ms: int = 0


class ReadinessData(BaseModel):
    status: Literal["ready", "not_ready"]
    duration_ms: int
    checks: list[CheckItem]


# --------------------------------------------------------------------------- #
# 鉴权
# --------------------------------------------------------------------------- #
class LoginRequest(BaseModel):
    user_id: str = Field(min_length=1, max_length=64)
    role: Role = Role.BUYER
    password: str | None = Field(default=None, max_length=128)


class TokenData(BaseModel):
    access_token: str
    token_type: Literal["bearer"] = "bearer"  # noqa: S105 - OAuth2 固定字面量，非凭证
    expires_in: int
    user_id: str
    role: Role


# --------------------------------------------------------------------------- #
# 售后
# --------------------------------------------------------------------------- #
class AfterSalesAction(StrEnum):
    REFUND = "refund"
    RETURN = "return"
    EXCHANGE = "exchange"


class ApplyRequest(BaseModel):
    order_id: str = Field(min_length=1, max_length=32)
    action: AfterSalesAction
    reason: str = Field(min_length=1, max_length=64)
    sku_id: str | None = Field(default=None, max_length=64)
    amount: float | None = Field(default=None, ge=0)
    confirm_token: str = Field(min_length=1, max_length=128, description="双确认令牌（单次有效）")


class ApplyData(BaseModel):
    biz_id: str
    status: str
    approval_required: bool


class CancelData(BaseModel):
    biz_id: str
    status: str
    version: int


class TimelineItem(BaseModel):
    status: str
    at: datetime


class AfterSalesDetail(BaseModel):
    biz_id: str
    action: AfterSalesAction
    status: str
    version: int
    order_id: str
    amount: float | None = None
    created_at: datetime
    timeline: list[TimelineItem] = Field(default_factory=list)


# --------------------------------------------------------------------------- #
# 审批
# --------------------------------------------------------------------------- #
class ApprovalCallbackRequest(BaseModel):
    approval_task_id: str = Field(min_length=1, max_length=64)
    decision: Literal["approve", "reject"]
    approver: str = Field(min_length=1, max_length=64)
    comment: str | None = Field(default=None, max_length=255)


class ApprovalCallbackData(BaseModel):
    approval_task_id: str
    biz_id: str
    decision: str
    status: str
    duplicated: bool = False


# --------------------------------------------------------------------------- #
# 评测 / Trace
# --------------------------------------------------------------------------- #
class EvalRunRequest(BaseModel):
    suite: str = Field(default="nightly", max_length=32)
    categories: list[EvalCategory] = Field(
        default_factory=lambda: list(EvalCategory),
        description="默认跑四类全量用例",
    )
    baseline: str | None = Field(default=None, max_length=32)


class EvalRunData(BaseModel):
    run_id: str
    status: Literal["queued", "running", "succeeded", "failed", "gate_failed"] = "succeeded"
    total: int = 0
    pass_cnt: int = 0
    completion_rate: float = 0.0
    citation_acc: float | None = None
    p95_ms: int | None = None
    gate_passed: bool = True
    attribution: dict[str, int] = Field(default_factory=dict)
    duration_ms: int = 0


class TraceSpan(BaseModel):
    span: str
    start_ms: float
    duration_ms: float
    status: str = "ok"
    tokens: int = 0
    meta: dict[str, Any] = Field(default_factory=dict)


class TraceData(BaseModel):
    trace_id: str
    spans: list[TraceSpan] = Field(default_factory=list)


# --------------------------------------------------------------------------- #
# 管理后台
# --------------------------------------------------------------------------- #
class PromptPublishRequest(BaseModel):
    version: int = Field(ge=1)


class IndexActivateData(BaseModel):
    version: str
    status: str
    invalidated_cache_prefix: str


class BreakerRequest(BaseModel):
    scope: Literal["write", "llm", "all"]
    open: bool


class BreakerData(BaseModel):
    scope: str
    open: bool
    effective_at: datetime


class GrayRequest(BaseModel):
    percent: int = Field(ge=0, le=100)


class GrayData(BaseModel):
    gray_percent: int
    updated_at: datetime


class DashboardData(BaseModel):
    resolution_rate: float = 0.0
    handoff_rate: float = 0.0
    intent_distribution: dict[str, int] = Field(default_factory=dict)
    p95_ms: float = 0.0
    error_rate: float = 0.0
    outbox_pending: int = 0
    llm_cost_cny: float = 0.0


class TakeoverData(BaseModel):
    session_id: str
    queue: str
    enqueued_at: datetime


class AuditItem(BaseModel):
    who: str
    what: str
    before: dict[str, Any] = Field(default_factory=dict)
    after: dict[str, Any] = Field(default_factory=dict)
    at: datetime


# --------------------------------------------------------------------------- #
# 订单（工作台只读，M2 接 Provider）
# --------------------------------------------------------------------------- #
class OrderLine(BaseModel):
    sku_id: str
    title: str
    qty: int
    amount: float


class OrderData(BaseModel):
    order_id: str
    user_id: str
    status: str
    created_at: datetime | None = None
    amount: float | None = None
    lines: list[OrderLine] = Field(default_factory=list)
