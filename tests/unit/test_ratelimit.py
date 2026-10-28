"""限流与会话互斥测试（FakeRedis，不依赖真实 Redis）。"""

from __future__ import annotations

from typing import Any

from app.services.ratelimit import RateLimiter


class FakeRedis:
    def __init__(self) -> None:
        self.sorted_sets: dict[str, dict[str, float]] = {}
        self.kv: dict[str, str] = {}
        self.fail = False

    async def zremrangebyscore(self, key: str, minimum: float, maximum: float) -> int:
        if self.fail:
            raise ConnectionError("down")
        items = self.sorted_sets.get(key, {})
        removed = [member for member, score in items.items() if minimum <= score <= maximum]
        for member in removed:
            items.pop(member, None)
        return len(removed)

    async def zadd(self, key: str, mapping: dict[str, float]) -> int:
        if self.fail:
            raise ConnectionError("down")
        self.sorted_sets.setdefault(key, {}).update(mapping)
        return len(mapping)

    async def zcard(self, key: str) -> int:
        return len(self.sorted_sets.get(key, {}))

    async def expire(self, key: str, seconds: int) -> bool:
        return True

    async def set(
        self, key: str, value: str, *, nx: bool = False, ex: int | None = None
    ) -> bool | None:
        if nx and key in self.kv:
            return None
        self.kv[key] = value
        return True

    async def delete(self, *keys: str) -> int:
        removed = 0
        for key in keys:
            if self.kv.pop(key, None) is not None:
                removed += 1
        return removed


async def test_allow_until_limit_then_deny() -> None:
    limiter = RateLimiter(FakeRedis(), limit_per_min=2)
    assert await limiter.allow("u1") == (True, 0)
    assert await limiter.allow("u1") == (True, 0)
    allowed, retry = await limiter.allow("u1")
    assert allowed is False
    assert retry == 60


async def test_limits_are_per_user() -> None:
    limiter = RateLimiter(FakeRedis(), limit_per_min=1)
    assert await limiter.allow("u1") == (True, 0)
    assert (await limiter.allow("u2"))[0] is True


async def test_redis_failure_degrades_open() -> None:
    redis = FakeRedis()
    redis.fail = True
    limiter = RateLimiter(redis, limit_per_min=1)
    assert await limiter.allow("u1") == (True, 0)


async def test_session_lock_is_exclusive_then_released() -> None:
    redis = FakeRedis()
    limiter = RateLimiter(redis, session_lock_ttl_seconds=30)
    assert await limiter.acquire_session("s1") is True
    assert await limiter.acquire_session("s1") is False
    assert await limiter.acquire_session("s2") is True
    await limiter.release_session("s1")
    assert await limiter.acquire_session("s1") is True


async def test_lock_degrades_open_on_redis_failure() -> None:
    redis = FakeRedis()

    async def boom(*args: Any, **kwargs: Any) -> Any:
        raise ConnectionError("down")

    redis.set = boom  # type: ignore[method-assign]
    limiter = RateLimiter(redis)
    assert await limiter.acquire_session("s1") is True
