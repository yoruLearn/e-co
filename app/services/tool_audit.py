"""工具调用审计（Spec §8.2 第 5 条）。

写 ``tool_audit``：trace_id / user_id / tool / level / params_hash / result_code / cost_ms。
审计失败只告警，不影响工具结果（审计是观测，不是业务前置）。
"""

from __future__ import annotations

from hashlib import sha256
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.common.logging import get_logger, log_warning
from app.db.models import ToolAudit

logger = get_logger("app.services.tool_audit")


def params_hash_of(params: dict[str, Any]) -> str:
    import json

    canonical = json.dumps(params, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return sha256(canonical.encode("utf-8")).hexdigest()


class ToolAuditRecorder:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = session_factory

    async def record(
        self,
        *,
        trace_id: str,
        user_id: str,
        tool: str,
        level: str,
        params: dict[str, Any],
        result_code: str = "OK",
        cost_ms: int = 0,
    ) -> None:
        try:
            async with self._sessions() as session:
                session.add(
                    ToolAudit(
                        trace_id=trace_id or "",
                        user_id=user_id,
                        tool=tool,
                        level=level,
                        params_hash=params_hash_of(params),
                        result_code=result_code,
                        cost_ms=max(0, cost_ms),
                    )
                )
                await session.commit()
        except Exception as exc:  # 审计失败不阻断业务
            log_warning(logger, "tool_audit.failed", reason=type(exc).__name__, tool=tool)


class NullToolAudit:
    async def record(self, **kwargs: Any) -> None:
        return None
