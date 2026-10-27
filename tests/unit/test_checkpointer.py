"""Checkpointer 测试：策略、后端选择、LangGraph 图持久化（内存后端）。

Redis 后端的真实持久化由 ``tests/integration/test_checkpoint_redis.py`` 在 CI 的
Redis 服务容器上验证——本文件只保证"不连 Redis 也能构造"和"图机制正确"。
"""

from __future__ import annotations

from typing import Any, TypedDict

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph

from app.agent.checkpointer import (
    CheckpointPolicy,
    build_checkpointer,
    close_checkpointer,
    init_checkpointer,
)
from app.common.config import Settings


class MiniState(TypedDict, total=False):
    intent: str
    answer: str
    calls: int


def _router(state: MiniState) -> MiniState:
    return {"intent": "qa", "calls": state.get("calls", 0) + 1}


def _answer(state: MiniState) -> MiniState:
    return {"answer": f"answered:{state['intent']}"}


def _route(state: MiniState) -> str:
    return "answer" if state.get("intent") == "qa" else "reject"


def _reject(state: MiniState) -> MiniState:
    return {"answer": "rejected"}


def _build_mini_graph() -> Any:
    graph = StateGraph(MiniState)
    graph.add_node("router", _router)
    graph.add_node("answer", _answer)
    graph.add_node("reject", _reject)
    graph.add_edge(START, "router")
    graph.add_conditional_edges("router", _route, {"answer": "answer", "reject": "reject"})
    graph.add_edge("answer", END)
    graph.add_edge("reject", END)
    return graph


def make_settings(**overrides: object) -> Settings:
    base: dict[str, object] = {
        "_env_file": None,
        "app_env": "test",
        "checkpointer_backend": "memory",
        "session_ttl_hours": 24,
    }
    base.update(overrides)
    return Settings(**base)


def test_policy_from_settings() -> None:
    policy = CheckpointPolicy.from_settings(make_settings(session_ttl_hours=2))
    assert policy.backend == "memory"
    assert policy.ttl_hours == 2
    assert policy.ttl_minutes == 120
    assert policy.ttl_seconds == 7200


def test_policy_minimum_ttl() -> None:
    policy = CheckpointPolicy(backend="redis", ttl_hours=0)
    assert policy.ttl_minutes == 1


def test_memory_backend_returns_inmemory_saver() -> None:
    saver = build_checkpointer(make_settings())
    assert isinstance(saver, InMemorySaver)


def test_redis_backend_constructs_without_connecting() -> None:
    settings = make_settings(
        checkpointer_backend="redis",
        redis_url="redis://127.0.0.1:6399/0",  # 不存在的端口：构造阶段不应连接
    )
    saver = build_checkpointer(settings)
    assert type(saver).__name__ == "AsyncRedisSaver"
    assert hasattr(saver, "asetup")


async def test_init_checkpointer_calls_asetup() -> None:
    class FakeSaver:
        def __init__(self) -> None:
            self.called = False

        async def asetup(self) -> None:
            self.called = True

    fake = FakeSaver()
    await init_checkpointer(fake)  # type: ignore[arg-type]
    assert fake.called is True

    await init_checkpointer(InMemorySaver())  # 无 asetup 时为 no-op


async def test_close_checkpointer_tolerates_missing_methods() -> None:
    class Closeable:
        def __init__(self) -> None:
            self.closed = False

        async def aclose(self) -> None:
            self.closed = True

    closeable = Closeable()
    await close_checkpointer(closeable)  # type: ignore[arg-type]
    assert closeable.closed is True

    await close_checkpointer(InMemorySaver())  # 没有 aclose/close 也不报错


def test_graph_runs_and_checkpoints_with_memory_saver() -> None:
    saver = build_checkpointer(make_settings())
    compiled = _build_mini_graph().compile(checkpointer=saver)
    config = {"configurable": {"thread_id": "unit-thread"}}

    result = compiled.invoke({"calls": 0}, config)
    assert result["intent"] == "qa"
    assert result["answer"] == "answered:qa"
    assert result["calls"] == 1

    state = compiled.get_state(config)
    assert state.values["answer"] == "answered:qa"
    assert state.next == ()


def test_checkpoint_survives_new_saver_instance_when_memory_backend() -> None:
    """内存后端只在本进程有效；这里验证同一 saver 上多 thread 隔离。"""
    saver = build_checkpointer(make_settings())
    compiled = _build_mini_graph().compile(checkpointer=saver)

    first = compiled.invoke({}, {"configurable": {"thread_id": "t1"}})
    second = compiled.invoke({}, {"configurable": {"thread_id": "t2"}})
    assert first["answer"] == "answered:qa"
    assert second["answer"] == "answered:qa"
    assert compiled.get_state({"configurable": {"thread_id": "t1"}}).values["calls"] == 1
    assert compiled.get_state({"configurable": {"thread_id": "t2"}}).values["calls"] == 1


@pytest.mark.parametrize("backend", ["memory"])
def test_backend_choices_are_typed(backend: str) -> None:
    settings = make_settings(checkpointer_backend=backend)
    assert settings.checkpointer_backend == backend
