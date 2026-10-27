"""评测结果落库（``eval_run`` 表）。失败只告警：评测结果丢失不应让接口 500。"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.common.logging import get_logger, log_warning
from app.db.models import EvalRun
from app.eval.executor import EvalReport

logger = get_logger("app.eval.store")


async def save_report(
    session_factory: async_sessionmaker[AsyncSession], report: EvalReport
) -> bool:
    now = datetime.now(tz=UTC).replace(tzinfo=None)
    try:
        async with session_factory() as session:
            session.add(
                EvalRun(
                    run_id=report.run_id,
                    suite=report.suite,
                    total=report.total,
                    pass_cnt=report.passed,
                    citation_acc=Decimal(str(round(report.citation_accuracy, 4))),
                    p95_ms=report.p95_ms,
                    report=report.as_dict(),
                    started_at=now,
                    finished_at=now,
                )
            )
            await session.commit()
        return True
    except Exception as exc:  # 落库失败只告警
        log_warning(logger, "eval.persist.failed", reason=type(exc).__name__)
        return False
