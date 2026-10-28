"""LangGraph 状态定义（Spec §5.3）。

字段与 Spec 逐项对齐；M0 只做类型与初始值工厂，图装配在 M1/M2。
"""

from __future__ import annotations

from enum import StrEnum
from typing import Literal, TypedDict

from pydantic import BaseModel, Field

from app.api.errors import ErrorCode


class Intent(StrEnum):
    QA = "qa"
    QUERY = "query"
    AFTER_SALES = "after_sales"
    CHITCHAT = "chitchat"


class ToolResult(BaseModel):
    name: str
    ok: bool
    data: dict[str, object] = Field(default_factory=dict)
    error_code: str | None = None
    cost_ms: int = 0
    degraded: bool = False


class Citation(BaseModel):
    """chunk 级引用（G2：无引用不得直出）。"""

    id: int
    doc_id: str
    title: str
    section: str
    snippet: str
    score: float = 0.0


class ErrorInfo(BaseModel):
    code: str = str(ErrorCode.INTERNAL)
    message: str = ""


class AfterSalesState(TypedDict, total=False):
    """图状态。``total=False`` 便于节点增量写入（LangGraph 允许部分更新）。"""

    trace_id: str
    session_id: str
    user_id: str
    user_query: str
    history: list[dict[str, str]]
    intent: str
    router_confidence: float
    rewritten_query: str | None
    slots: dict[str, object]
    missing_slots: list[str]
    tool_results: list[ToolResult]
    citations: list[dict[str, object]]
    context_blocks: list[str]
    band: str
    degraded: list[str]
    index_version: str
    biz_id: str | None
    action: Literal["refund", "return", "exchange"] | None
    status: str
    version: int
    confirm_required: bool
    approval_task_id: str | None
    answer_stream: str
    cache_hit: bool
    user_role: str
    user_confirm_token: str | None
    confirm_token: str | None
    pending_action: dict[str, object] | None
    usage_tokens: int
    error: ErrorInfo | None


def initial_state(
    *,
    trace_id: str,
    session_id: str,
    user_id: str,
    user_query: str = "",
    history: list[dict[str, str]] | None = None,
    user_role: str = "buyer",
    confirm_token: str | None = None,
    pending_action: dict[str, object] | None = None,
) -> AfterSalesState:
    """新建会话的初始状态；``version=0`` 与 DB 乐观锁初值一致。"""
    return AfterSalesState(
        trace_id=trace_id,
        session_id=session_id,
        user_id=user_id,
        user_query=user_query,
        history=list(history or []),
        user_role=user_role,
        user_confirm_token=confirm_token,
        intent=Intent.QA.value,
        router_confidence=0.0,
        rewritten_query=None,
        slots={},
        missing_slots=[],
        tool_results=[],
        citations=[],
        context_blocks=[],
        band="",
        degraded=[],
        index_version="",
        biz_id=None,
        action=None,
        status="DRAFT",
        version=0,
        confirm_required=False,
        approval_task_id=None,
        answer_stream="",
        cache_hit=False,
        confirm_token=None,
        pending_action=pending_action,
        usage_tokens=0,
        error=None,
    )
