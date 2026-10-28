"""链路查询（Spec §9.2 ``GET /traces/{trace_id}``，agent/admin；G6 全链路可回放）。

M1 起返回真实节点级调用树（``trace_span``），按 ``start_ms`` 还原执行顺序；
trace 不存在返回 404（而不是空数组），避免"以为有数据"。
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request

from app.api.deps import require_roles
from app.api.errors import AppError, ErrorCode
from app.api.responses import ERROR_RESPONSES, SuccessEnvelope, ok
from app.api.schemas import TraceData, TraceSpan
from app.common.security import Principal, Role

router = APIRouter(prefix="/traces", tags=["traces"], responses=ERROR_RESPONSES)

TraceReader = Annotated[Principal, Depends(require_roles(Role.AGENT, Role.ADMIN))]


@router.get(
    "/{trace_id}",
    summary="按 trace_id 还原调用树",
    response_model=SuccessEnvelope[TraceData],
)
async def get_trace(
    trace_id: str,
    principal: TraceReader,
    request: Request,
) -> dict[str, Any]:
    runtime = request.app.state.agent
    spans = await runtime.trace.fetch(trace_id)
    if not spans:
        raise AppError(
            ErrorCode.NOT_FOUND,
            "trace 不存在或已过期",
            detail={"trace_id": trace_id},
        )
    data = TraceData(
        trace_id=trace_id,
        spans=[TraceSpan(**span) for span in spans],
    )
    return ok(data.model_dump())
