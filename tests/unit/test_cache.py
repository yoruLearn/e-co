"""两级缓存与单飞测试（FakeRedis，不依赖真实 Redis）。"""

from __future__ import annotations

import asyncio
import fnmatch
from typing import Any

import pytest

from app.cache.two_level import LocalTTLCache, TwoLevelCache


class FakeRedis:
    def __init__(self) -> None:
        self.data: dict[str, Any] = {}
        self.calls = {"get": 0, "set": 0}
        self.fail = False

    async def get(self, key: str) -> Any:
        self.calls["get"] += 1
        if self.fail:
            raise ConnectionError("redis down")
        return self.data.get(key)

    async def set(self, key: str, value: Any, ex: int | None = None) -> bool:
        self.calls["set"] += 1
        if self.fail:
            raise ConnectionError("redis down")
        self.data[key] = value
        return True

    async def delete(self, *keys: str) -> int:
        removed = 0
        for key in keys:
            if self.data.pop(key, None) is not None:
                removed += 1
        return removed

    async def scan(self, *, cursor: int = 0, match: str | None = None, count: int = 200):
        keys = [key for key in self.data if match is None or fnmatch.fnmatch(key, match)]
        return 0, keys

    async def aclose(self) -> None:
        return None


def test_local_cache_hit_miss_and_expiry() -> None:
    cache = LocalTTLCache(ttl_seconds=60)
    cache.set("a", {"v": 1})
    assert cache.get("a") == {"v": 1}
    assert cache.get("missing") is None

    expired = LocalTTLCache(ttl_seconds=0)
    expired.set("a", 1)
    assert expired.get("a") is None


def test_local_cache_prefix_delete_and_capacity() -> None:
    cache = LocalTTLCache(ttl_seconds=60, max_entries=2)
    cache.set("rag:v1:a", 1)
    cache.set("rag:v1:b", 2)
    cache.set("rag:v2:c", 3)  # 触发 FIFO 淘汰
    assert len(cache) == 2
    assert cache.delete_prefix("rag:v1:") == 1
    assert cache.get("rag:v1:b") is None


async def test_two_level_cache_reads_redis_after_local_miss() -> None:
    redis = FakeRedis()
    cache = TwoLevelCache(redis, local_ttl_seconds=0, redis_ttl_seconds=60)
    await cache.set("k", {"x": 1})
    # 本地 TTL=0 已过期 → 应从 Redis 命中
    assert await cache.get("k") == {"x": 1}
    assert cache.stats["l2"] == 1


async def test_two_level_cache_survives_redis_failure() -> None:
    redis = FakeRedis()
    redis.fail = True
    cache = TwoLevelCache(redis)
    assert await cache.get("k") is None
    await cache.set("k", 1)  # 不抛异常
    assert len(cache._local) == 1  # 本地仍然写入


async def test_invalidate_prefix_clears_local_and_redis() -> None:
    redis = FakeRedis()
    cache = TwoLevelCache(redis, local_ttl_seconds=60)
    await cache.set("rag:v1:q1", 1)
    await cache.set("rag:v2:q1", 2)
    removed = await cache.invalidate_prefix("rag:v1:")
    assert removed >= 1
    assert cache._local.get("rag:v1:q1") is None
    assert "cache:rag:v2:q1" in redis.data


async def test_singleflight_hits_factory_once() -> None:
    redis = FakeRedis()
    cache = TwoLevelCache(redis)
    calls = {"n": 0}

    async def factory() -> dict[str, int]:
        calls["n"] += 1
        await asyncio.sleep(0.02)
        return {"value": 42}

    results = await asyncio.gather(*(cache.get_or_compute("hot", factory) for _ in range(8)))
    assert calls["n"] == 1, "同 key 并发只能回源一次"
    assert all(result == {"value": 42} for result in results)


async def test_singleflight_returns_cached_value_on_second_call() -> None:
    cache = TwoLevelCache(FakeRedis())
    calls = {"n": 0}

    async def factory() -> int:
        calls["n"] += 1
        return 7

    assert await cache.get_or_compute("k", factory) == 7
    assert await cache.get_or_compute("k", factory) == 7
    assert calls["n"] == 1


async def test_singleflight_degrades_on_timeout(monkeypatch) -> None:
    import app.cache.two_level as module

    monkeypatch.setattr(module, "LOCK_WAIT_TIMEOUT_S", 0.01)
    cache = TwoLevelCache(FakeRedis())
    started = asyncio.Event()
    release = asyncio.Event()

    async def slow() -> str:
        started.set()
        await release.wait()
        return "slow"

    slow_task = asyncio.create_task(cache.get_or_compute("k", slow))
    await started.wait()
    assert await cache.get_or_compute("k", slow, degrade_value="degraded") == "degraded"
    release.set()
    assert await slow_task == "slow"


async def test_factory_none_returns_degrade_value() -> None:
    cache = TwoLevelCache(FakeRedis())

    async def factory() -> None:
        return None

    assert await cache.get_or_compute("k", factory, degrade_value="x") == "x"


@pytest.mark.parametrize("ttl", [60, 300])
def test_cache_key_namespace(ttl: int) -> None:
    cache = TwoLevelCache(FakeRedis(), redis_ttl_seconds=ttl)
    assert cache._redis_ttl == ttl
