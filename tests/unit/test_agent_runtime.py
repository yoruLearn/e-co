"""AgentRuntime 装配/关闭/版本切换测试（不依赖外部服务）。"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from app.agent.runtime import AgentRuntime, build_agent_runtime
from app.cache.two_level import TwoLevelCache
from app.common.config import REPO_ROOT, Settings


class FakeRedis:
    def __init__(self) -> None:
        self.data: dict[str, Any] = {}
        self.deleted_prefixes: list[str] = []

    async def get(self, key: str) -> Any:
        return self.data.get(key)

    async def set(self, key: str, value: str, ex: int | None = None) -> bool:
        self.data[key] = value
        return True

    async def delete(self, *keys: str) -> int:
        return sum(1 for key in keys if self.data.pop(key, None) is not None)

    async def scan(self, *, cursor: int = 0, match: str | None = None, count: int = 200):
        return 0, list(self.data)


def make_settings(**overrides: object) -> Settings:
    base: dict[str, object] = {
        "_env_file": None,
        "app_env": "test",
        "log_level": "WARNING",
        "log_format": "console",
        "rag_offline": True,
        "prompt_seed_path": str(REPO_ROOT / "seeds" / "prompts.yaml"),
        "checkpointer_backend": "memory",
        "llm_provider": "mock",
    }
    base.update(overrides)
    return Settings(**base)


def test_build_agent_runtime_offline_wires_everything() -> None:
    settings = make_settings()
    clients = SimpleNamespace(redis=FakeRedis(), engine=None, session_factory=None)
    runtime = build_agent_runtime(settings, clients)  # type: ignore[arg-type]
    try:
        assert isinstance(runtime, AgentRuntime)
        assert runtime.graph is not None
        assert runtime.cache is not None
        assert runtime.answerability is not None
        assert runtime.trace is not None
        assert runtime.sse_buffer is not None
    finally:
        import asyncio

        asyncio.run(runtime.close())


def test_active_index_version_falls_back_to_settings() -> None:
    import asyncio

    settings = make_settings(rag_index_version="v-test")
    clients = SimpleNamespace(redis=FakeRedis(), engine=None, session_factory=None)
    runtime = build_agent_runtime(settings, clients)  # type: ignore[arg-type]
    try:
        assert asyncio.run(runtime.active_index_version()) == "v-test"
    finally:
        asyncio.run(runtime.close())


def test_version_switch_invalidates_cache_prefix() -> None:
    import asyncio

    settings = make_settings()
    redis = FakeRedis()
    clients = SimpleNamespace(redis=redis, engine=None, session_factory=None)
    runtime = build_agent_runtime(settings, clients)  # type: ignore[arg-type]
    try:
        runtime.cache = TwoLevelCache(redis)  # type: ignore[assignment]
        asyncio.run(runtime.active_index_version())  # v1，首次记录
        runtime._last_index_version = "v0"
        asyncio.run(runtime.active_index_version())
        assert runtime._last_index_version == settings.rag_index_version
    finally:
        asyncio.run(runtime.close())


class FakeSession:
    def __init__(self, row: Any) -> None:
        self._row = row

    async def scalar(self, statement: Any) -> Any:
        return self._row

    async def __aenter__(self) -> FakeSession:
        return self

    async def __aexit__(self, *args: Any) -> None:
        return None


class FakeSessionFactory:
    def __init__(self, row: Any) -> None:
        self._row = row

    def __call__(self) -> FakeSession:
        return FakeSession(self._row)


class FailingVectorIndex:
    def __init__(self) -> None:
        self.closed = False

    async def create_collection(self, name: str) -> str:
        raise RuntimeError("chroma down")

    async def aclose(self) -> None:
        self.closed = True


class BrokenCheckpointer:
    async def asetup(self) -> None:
        raise RuntimeError("redis down")


def test_active_index_version_reads_db_row() -> None:
    import asyncio
    from types import SimpleNamespace

    settings = make_settings()
    clients = SimpleNamespace(
        redis=FakeRedis(),
        engine=None,
        session_factory=FakeSessionFactory(SimpleNamespace(version="v9")),
    )
    runtime = build_agent_runtime(settings, clients)  # type: ignore[arg-type]
    try:
        assert asyncio.run(runtime.active_index_version()) == "v9"
        assert runtime._last_index_version == "v9"
    finally:
        asyncio.run(runtime.close())


def test_collection_id_handles_failure_and_cache() -> None:
    import asyncio
    from types import SimpleNamespace

    settings = make_settings()
    runtime = build_agent_runtime(
        settings,
        SimpleNamespace(redis=FakeRedis(), engine=None, session_factory=None),  # type: ignore[arg-type]
    )
    try:
        failing = FailingVectorIndex()
        runtime.vector_index = failing  # type: ignore[assignment]
        assert asyncio.run(runtime.collection_id("v1")) is None
        runtime._collection_cache["v1"] = "cached-id"
        assert asyncio.run(runtime.collection_id("v1")) == "cached-id"
        asyncio.run(runtime.close())
        assert failing.closed is True
    finally:
        pass


def test_null_vector_index_is_noop() -> None:
    import asyncio

    from app.agent.runtime import _NullVectorIndex

    index = _NullVectorIndex()
    assert asyncio.run(index.create_collection("v1")) == "offline-v1"
    assert asyncio.run(index.search("c", [0.1], top_k=3)) == []
    assert asyncio.run(index.upsert()) is None
    assert asyncio.run(index.delete_collection("v1")) is None


def test_init_agent_runtime_falls_back_to_memory() -> None:
    import asyncio
    from types import SimpleNamespace

    settings = make_settings()
    runtime = build_agent_runtime(
        settings,
        SimpleNamespace(redis=FakeRedis(), engine=None, session_factory=None),  # type: ignore[arg-type]
    )
    runtime.checkpointer = BrokenCheckpointer()
    asyncio.run(
        __import__("app.agent.runtime", fromlist=["init_agent_runtime"]).init_agent_runtime(runtime)
    )
    from langgraph.checkpoint.memory import InMemorySaver

    assert isinstance(runtime.checkpointer, InMemorySaver)
    assert runtime.graph is not None
