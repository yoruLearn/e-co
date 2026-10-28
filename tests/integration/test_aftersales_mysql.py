"""M2 MySQL 集成测试：并发幂等、迁移合法性、审批回调幂等、恢复。

本机无 Docker 时不跑；CI 的 ``aftersales-mysql`` job 通过 ``MYSQL_TESTS_URL`` 启用。
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.api.errors import AppError
from app.common.config import Settings
from app.db.models import AfterSalesBiz, ApprovalTask, Base
from app.services.aftersales import AfterSalesService, ApplyCommand
from app.services.approval import ApprovalService
from app.services.state_machine import AfterSalesEvent, AfterSalesStatus
from app.services.transition import transition_biz

MYSQL_URL = os.getenv("MYSQL_TESTS_URL")
pytestmark = [
    pytest.mark.mysql,
    pytest.mark.skipif(not MYSQL_URL, reason="需要 MYSQL_TESTS_URL（CI service job）"),
]


@pytest_asyncio.fixture()
async def session_factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    assert MYSQL_URL
    engine = create_async_engine(MYSQL_URL, pool_pre_ping=True, pool_size=20, max_overflow=10)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        yield factory
    finally:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)
        await engine.dispose()


def make_settings() -> Settings:
    return Settings(_env_file=None, app_env="test", approval_threshold_cny=500.0)


def apply_cmd(*, idem: str, amount: str = "199.00", user: str = "u1") -> ApplyCommand:
    from decimal import Decimal

    return ApplyCommand(
        user_id=user,
        order_id="O20261027001",
        action="refund",
        reason_code="quality_issue",
        amount=Decimal(amount),
        trace_id="tr_mysql",
        idem_key=idem,
    )


async def test_concurrent_apply_creates_one_biz(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    service = AfterSalesService(session_factory, settings=make_settings())
    outcomes = await asyncio.gather(
        *(service.apply(apply_cmd(idem="idem-concurrent-1")) for _ in range(10))
    )
    biz_ids = {outcome.biz_id for outcome in outcomes}
    assert len(biz_ids) == 1, "并发 10 次必须只产生 1 张单据"
    async with session_factory() as session:
        count = await session.scalar(select(func.count()).select_from(AfterSalesBiz))
    assert count == 1


async def test_apply_replays_and_conflicts(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    service = AfterSalesService(session_factory, settings=make_settings())
    first = await service.apply(apply_cmd(idem="idem-replay-1"))
    again = await service.apply(apply_cmd(idem="idem-replay-1"))
    assert again.biz_id == first.biz_id
    with pytest.raises(AppError) as exc:
        await service.apply(apply_cmd(idem="idem-replay-1", amount="99.00"))
    assert exc.value.code == "AS-4004"


async def test_illegal_transition_and_version_conflict(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    service = AfterSalesService(session_factory, settings=make_settings())
    outcome = await service.apply(apply_cmd(idem="idem-transition-1"))
    assert outcome.status == "APPROVED"

    async with session_factory() as session, session.begin():
        biz = await session.scalar(
            select(AfterSalesBiz).where(AfterSalesBiz.biz_id == outcome.biz_id)
        )
        assert biz is not None
        await transition_biz(session, biz, AfterSalesEvent.EXECUTE, actor="worker", trace_id="tr")
        assert biz.status == "PROCESSING"
        with pytest.raises(AppError) as exc:
            await transition_biz(
                session, biz, AfterSalesEvent.EXECUTE, actor="worker", trace_id="tr"
            )
        assert exc.value.code == "AS-4003"

    async with session_factory() as session:
        async with session.begin():
            stale_a = await session.scalar(
                select(AfterSalesBiz).where(AfterSalesBiz.biz_id == outcome.biz_id)
            )
            stale_b = await session.scalar(
                select(AfterSalesBiz).where(AfterSalesBiz.biz_id == outcome.biz_id)
            )
            assert stale_a is not None and stale_b is not None
            await transition_biz(
                session, stale_a, AfterSalesEvent.COMPLETE, actor="worker", trace_id="tr"
            )
        with pytest.raises(AppError) as exc2:
            async with session.begin():
                await transition_biz(
                    session, stale_b, AfterSalesEvent.FAIL, actor="worker", trace_id="tr"
                )
        assert exc2.value.code == "AS-4003"


async def test_approval_callback_is_idempotent(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    settings = make_settings()
    service = AfterSalesService(session_factory, settings=settings)
    outcome = await service.apply(apply_cmd(idem="idem-approval-1", amount="600.00"))
    assert outcome.status == "PENDING_APPROVAL"
    assert outcome.approval_required is True

    async with session_factory() as session:
        task = await session.scalar(
            select(ApprovalTask).where(ApprovalTask.biz_id == outcome.biz_id)
        )
    assert task is not None

    approval = ApprovalService(session_factory, settings=settings)
    first = await approval.callback(
        task_id=task.task_id,
        decision="approve",
        approver="u_889",
        comment="符合七天无理由",
        trace_id="tr",
    )
    second = await approval.callback(
        task_id=task.task_id,
        decision="approve",
        approver="u_889",
        comment="符合七天无理由",
        trace_id="tr",
    )
    assert first["status"] == "APPROVED"
    assert first["duplicated"] is False
    assert second["duplicated"] is True
    assert second["status"] == "APPROVED"


async def test_rejection_is_visible_after_callback(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    settings = make_settings()
    service = AfterSalesService(session_factory, settings=settings)
    outcome = await service.apply(apply_cmd(idem="idem-reject-1", amount="700.00"))
    async with session_factory() as session:
        task = await session.scalar(
            select(ApprovalTask).where(ApprovalTask.biz_id == outcome.biz_id)
        )
    assert task is not None
    approval = ApprovalService(session_factory, settings=settings)
    await approval.callback(
        task_id=task.task_id, decision="reject", approver="u_889", comment="超范围", trace_id="tr"
    )
    detail = await service.get(outcome.biz_id, user_id="u1", role="buyer")
    assert detail["status"] == str(AfterSalesStatus.REJECTED)
    assert detail["timeline"][-1]["status"] == "REJECTED"
