"""链路查询（Spec §9.2 ``GET /traces/{trace_id}``，agent/admin，G6 全链路可回放）。

M0 只挂契约。M1 起由结构化日志/``tool_audit``/MQ 归档聚合出调用树。
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends

from app.api.deps import require_roles
from app.api.errors import not_implemented
from app.api.responses import ERROR_RESPONSES, SuccessEnvelope
from app.api.schemas import TraceData
from app.common.security import Principal, Role

router = APIRouter(prefix="/traces", tags=["traces"], responses=ERROR_RESPONSES)

TraceReader = Annotated[Principal, Depends(require_roles(Role.AGENT, Role.ADMIN))]


@router.get(
    "/{trace_id}",
    summary="按 trace_id 还原调用树",
    response_model=SuccessEnvelope[TraceData],
)
async def get_trace(trace_id: str, principal: TraceReader) -> dict[str, Any]:
    # TODO(M1): api → node → tool → rag → mq → db 各段耗时/token/缓存命中
    not_implemented("trace 调用树查询", "M1")
