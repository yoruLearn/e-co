"""健康探针（Spec §9.2 / §17.6）。

- ``/healthz``：**存活**。不访问任何外部依赖，毫秒级返回，供 k8s liveness / compose healthcheck；
- ``/readyz``：**就绪**。并发校验 MySQL/Redis/MQ/Chroma 连通 + 迁移版本，任一失败返回 503
  并在 ``checks[]`` 给出逐项明细（不伪装健康）。

探针与业务 API 共用统一响应信封，便于运维脚本按 ``code`` 判断。
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends

from app.api.deps import ReadinessDep, SettingsDep, get_uptime_seconds
from app.api.errors import AppError, ErrorCode
from app.api.responses import ERROR_RESPONSES, SuccessEnvelope, ok
from app.api.schemas import CheckItem, HealthData, ReadinessData

router = APIRouter(tags=["ops"], responses=ERROR_RESPONSES)


@router.get(
    "/healthz",
    summary="存活探针",
    response_model=SuccessEnvelope[HealthData],
)
async def healthz(
    settings: SettingsDep,
    uptime: Annotated[float, Depends(get_uptime_seconds)],
) -> dict[str, Any]:
    data = HealthData(
        version=settings.app_version,
        env=settings.app_env,
        uptime_s=round(uptime, 3),
    )
    return ok(data.model_dump())


@router.get(
    "/readyz",
    summary="就绪探针（依赖连通 + 迁移版本）",
    response_model=SuccessEnvelope[ReadinessData],
    responses={503: ERROR_RESPONSES[503]},
)
async def readyz(service: ReadinessDep) -> dict[str, Any]:
    report = await service.check()
    payload = report.as_dict()
    checks = [CheckItem.model_validate(item).model_dump() for item in payload["checks"]]
    if not report.ready:
        raise AppError(
            ErrorCode.DEPENDENCY_UNAVAILABLE,
            "依赖未就绪",
            detail={
                "status": "not_ready",
                "duration_ms": report.duration_ms,
                "checks": checks,
            },
            http_status=503,
        )
    return ok(
        ReadinessData(
            status="ready",
            duration_ms=report.duration_ms,
            checks=[CheckItem.model_validate(item) for item in payload["checks"]],
        ).model_dump()
    )
