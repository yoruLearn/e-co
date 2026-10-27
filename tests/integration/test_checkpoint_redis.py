"""Redis checkpointer 真实持久化（CI 的 RAG job 提供 Redis）。

验证 Spec §6.2 第 5 条 / FR-AS-06 的核心诉求：**跨进程/跨实例恢复**。
这里用两个独立的 ``AsyncRedisSaver`` 实例模拟两个进程。
"""

from __future__ import annotations

import os
import uuid
from typing import TYPE_CHECKING, Any, TypedDict

import pytest

from app.agent.checkpointer import build_checkpointer, close_checkpointer, init_checkpointer
from app.common.config import Settings

if TYPE_CHECKING:
    from redis.asyncio import Redis

pytestmark = pytest.mark.integration_redis


class MiniState(TypedDict, total=False):
    step: str
    answer: str


def _prepare(state: MiniState) -> MiniState:
    return {"step": "prepared"}


def _finish(state: MiniState) -> MiniState:
    return {"answer": f"done:{state['step']}"}


def _graph() -> Any:
    from langgraph.graph import END, START, StateGraph

    graph = StateGraph(MiniState)
    graph.add_node("prepare", _prepare)
    graph.add_node("finish", _finish)
    graph.add_edge(START, "prepare")
    graph.add_edge("prepare", "finish")
    graph.add_edge("finish", END)
    return graph


async def _redis_available(url: str) -> bool:
    from redis.asyncio import Redis

    client: Redis = Redis.from_url(url, socket_connect_timeout=1, socket_timeout=1)
    try:
        return bool(await client.ping())
    except Exception:
        return False
    finally:
        await client.aclose()


@pytest.fixture()
async def settings() -> Settings:
    url = os.environ.get("REDIS_URL", "redis://127.0.0.1:6379/0")
    if not await _redis_available(url):
        pytest.skip(f"Redis 不可达：{url}")
    return Settings(
        _env_file=None,
        app_env="test",
        checkpointer_backend="redis",
        redis_url=url,
        session_ttl_hours=1,
    )


async def test_checkpoint_persists_across_saver_instances(settings: Settings) -> None:
    thread_id = f"ci-{uuid.uuid4().hex[:12]}"
    config = {"configurable": {"thread_id": thread_id}}

    first = build_checkpointer(settings)
    await init_checkpointer(first)
    try:
        await _graph().compile(checkpointer=first).ainvoke({}, config)
    finally:
        await close_checkpointer(first)

    # 新实例 = 新进程：从 Redis 读回同一 thread 的 checkpoint
    second = build_checkpointer(settings)
    await init_checkpointer(second)
    try:
        compiled = _graph().compile(checkpointer=second)
        state = await compiled.aget_state(config)
        assert state.values["answer"] == "done:prepared", "checkpoint 未跨实例恢复"
    finally:
        await close_checkpointer(second)
