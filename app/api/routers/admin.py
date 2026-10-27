"""管理后台 API（Spec §9.2 / §18，ops/admin，全部变更写审计）。

M0 只挂契约。M3 实现：秒级生效的熔断/灰度开关、Prompt 与索引版本发布、
看板聚合、人工接管、变更审计（who/what/before/after）。
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends

from app.api.deps import require_roles
from app.api.errors import not_implemented
from app.api.responses import ERROR_RESPONSES, SuccessEnvelope
from app.api.schemas import (
    AuditItem,
    BreakerData,
    BreakerRequest,
    DashboardData,
    GrayData,
    GrayRequest,
    IndexActivateData,
    PromptPublishRequest,
    TakeoverData,
)
from app.common.security import Principal, Role

router = APIRouter(prefix="/admin", tags=["admin"], responses=ERROR_RESPONSES)

Operator = Annotated[Principal, Depends(require_roles(Role.OPS, Role.ADMIN))]


@router.post(
    "/prompts/{key}/publish",
    summary="发布/回滚 Prompt 版本（评测门禁）",
    response_model=SuccessEnvelope[dict[str, Any]],
)
async def publish_prompt(
    key: str,
    payload: PromptPublishRequest,
    principal: Operator,
) -> dict[str, Any]:
    # TODO(M3): eval_gate 校验 → 置 published → 旧版 deprecated → 进程内缓存失效
    not_implemented("Prompt 发布/回滚", "M3")


@router.post(
    "/index/{version}/activate",
    summary="激活/回切索引版本（联动缓存失效）",
    response_model=SuccessEnvelope[IndexActivateData],
)
async def activate_index(version: str, principal: Operator) -> dict[str, Any]:
    # TODO(M3): index_version 状态流转 + DEL rag:{old_ver}:* + idx:active 更新
    not_implemented("索引版本激活/回切", "M3")


@router.post(
    "/breaker",
    summary="熔断开关（秒级生效）",
    response_model=SuccessEnvelope[BreakerData],
)
async def set_breaker(payload: BreakerRequest, principal: Operator) -> dict[str, Any]:
    # TODO(M3): 写 Redis 开关 + 审计 + 话术自动转人工
    not_implemented("熔断开关", "M3")


@router.post(
    "/gray",
    summary="设置灰度放量百分比（0-100 实时生效）",
    response_model=SuccessEnvelope[GrayData],
)
async def set_gray(payload: GrayRequest, principal: Operator) -> dict[str, Any]:
    # TODO(M3): 灰度比例落 Redis，按 hash(user_id)%100 分流
    not_implemented("灰度放量控制", "M3")


@router.get(
    "/dashboard",
    summary="看板聚合（业务 + 工程指标）",
    response_model=SuccessEnvelope[DashboardData],
)
async def dashboard(principal: Operator) -> dict[str, Any]:
    # TODO(M3): 解决率/转人工率/意图分布/P95/错误率/outbox 积压/LLM 成本
    not_implemented("运营看板", "M3")


@router.post(
    "/sessions/{session_id}/takeover",
    summary="会话转入人工队列",
    response_model=SuccessEnvelope[TakeoverData],
)
async def takeover(session_id: str, principal: Operator) -> dict[str, Any]:
    # TODO(M3): 熔断/兜底/超时会话入队 + AS_NOTIFY
    not_implemented("人工接管", "M3")


@router.get(
    "/audit",
    summary="变更审计查询（保留 ≥180 天）",
    response_model=SuccessEnvelope[list[AuditItem]],
)
async def audit(principal: Operator) -> dict[str, Any]:
    # TODO(M3): 审计表查询（who/what/before/after）
    not_implemented("变更审计查询", "M3")
