"""数据库会话工厂（SQLAlchemy 2.0 async）。

事务边界遵循 Spec §10.2：**事务内不发网络请求**，消息投递交给 outbox dispatcher。
M0 只提供会话工厂，业务事务在 M2 实现。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.common.clients import AppClients


def session_factory(clients: AppClients) -> async_sessionmaker[AsyncSession]:
    return clients.session_factory


@asynccontextmanager
async def session_scope(
    factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[AsyncSession]:
    """提交/回滚语义包装：异常回滚，正常提交。"""
    async with factory() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise
        else:
            await session.commit()
