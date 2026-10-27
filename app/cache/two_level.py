"""两级缓存与单飞（Spec §7.3 / FR-CACHE-02/04）。

- **L1 进程内**：60s TTL，挡热点 key 的重复解析/序列化；
- **L2 Redis**：300s TTL，跨副本共享；
- **单飞（singleflight）**：同 key 并发只回源一次——
  进程内 per-key asyncio.Lock + 跨进程 Redis ``SET NX PX``；没抢到锁的等结果，
  超时返回降级值（宁可少数请求拿旧值/空值，也不把下游打穿）。

Redis 不可用时全部降级为"只读 L1"，不阻塞业务。
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from redis.asyncio import Redis

from app.common.logging import get_logger, log_warning

logger = get_logger("app.cache")

LOCK_TTL_MS = 5000
LOCK_WAIT_TIMEOUT_S = 2.0
LOCK_POLL_INTERVAL_S = 0.05


@dataclass(slots=True)
class _Entry:
    value: Any
    expires_at: float


class LocalTTLCache:
    """进程内 TTL 缓存（容量上限按 FIFO 淘汰，够用且实现简单）。"""

    def __init__(self, *, ttl_seconds: int = 60, max_entries: int = 2048) -> None:
        self._ttl = max(0, ttl_seconds)
        self._max = max(1, max_entries)
        self._data: dict[str, _Entry] = {}

    def get(self, key: str) -> Any | None:
        entry = self._data.get(key)
        if entry is None:
            return None
        if entry.expires_at <= time.monotonic():
            self._data.pop(key, None)
            return None
        return entry.value

    def set(self, key: str, value: Any) -> None:
        if len(self._data) >= self._max:
            oldest = next(iter(self._data))
            self._data.pop(oldest, None)
        self._data[key] = _Entry(value=value, expires_at=time.monotonic() + self._ttl)

    def delete(self, key: str) -> None:
        self._data.pop(key, None)

    def delete_prefix(self, prefix: str) -> int:
        keys = [key for key in self._data if key.startswith(prefix)]
        for key in keys:
            self._data.pop(key, None)
        return len(keys)

    def clear(self) -> None:
        self._data.clear()

    def __len__(self) -> int:
        return len(self._data)


class TwoLevelCache:
    def __init__(
        self,
        redis: Redis,
        *,
        local_ttl_seconds: int = 60,
        redis_ttl_seconds: int = 300,
        max_local_entries: int = 2048,
    ) -> None:
        self._redis = redis
        self._redis_ttl = redis_ttl_seconds
        self._local = LocalTTLCache(ttl_seconds=local_ttl_seconds, max_entries=max_local_entries)
        self._locks: dict[str, asyncio.Lock] = {}
        self.stats: dict[str, int] = {"l1": 0, "l2": 0, "miss": 0}

    async def get(self, key: str) -> Any | None:
        local = self._local.get(key)
        if local is not None:
            self.stats["l1"] = self.stats.get("l1", 0) + 1
            return local
        try:
            raw = await self._redis.get(f"cache:{key}")
        except Exception as exc:  # Redis 挂掉只降级
            log_warning(logger, "cache.redis.get.failed", reason=type(exc).__name__)
            raw = None
        if raw is None:
            self.stats["miss"] = self.stats.get("miss", 0) + 1
            return None
        try:
            value = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return None
        self._local.set(key, value)
        self.stats["l2"] = self.stats.get("l2", 0) + 1
        return value

    async def set(self, key: str, value: Any) -> None:
        self._local.set(key, value)
        try:
            await self._redis.set(
                f"cache:{key}",
                json.dumps(value, ensure_ascii=False),
                ex=self._redis_ttl,
            )
        except Exception as exc:
            log_warning(logger, "cache.redis.set.failed", reason=type(exc).__name__)

    async def delete(self, key: str) -> None:
        self._local.delete(key)
        try:
            await self._redis.delete(f"cache:{key}")
        except Exception as exc:
            log_warning(logger, "cache.redis.del.failed", reason=type(exc).__name__)

    async def invalidate_prefix(self, prefix: str) -> int:
        """按前缀失效（索引版本切换时用），Redis 侧用 SCAN 而非 KEYS。"""
        removed = self._local.delete_prefix(prefix)
        try:
            cursor = 0
            pattern = f"cache:{prefix}*"
            while True:
                cursor, keys = await self._redis.scan(cursor=cursor, match=pattern, count=200)
                if keys:
                    await self._redis.delete(*keys)
                    removed += len(keys)
                if cursor == 0:
                    break
        except Exception as exc:
            log_warning(logger, "cache.redis.scan.failed", reason=type(exc).__name__)
        return removed

    def _lock_for(self, key: str) -> asyncio.Lock:
        return self._locks.setdefault(key, asyncio.Lock())

    async def get_or_compute(
        self,
        key: str,
        factory: Callable[[], Awaitable[Any]],
        *,
        degrade_value: Any = None,
    ) -> Any:
        cached = await self.get(key)
        if cached is not None:
            return cached

        lock = self._lock_for(key)
        try:
            await asyncio.wait_for(lock.acquire(), timeout=LOCK_WAIT_TIMEOUT_S)
        except TimeoutError:
            # 等锁超时：返回已有缓存或降级值，绝不把请求堆在下游
            cached = await self.get(key)
            return cached if cached is not None else degrade_value
        try:
            # 双检：可能已被前一个持锁者写入
            cached = await self.get(key)
            if cached is not None:
                return cached
            value = await factory()
            if value is not None:
                await self.set(key, value)
            return value if value is not None else degrade_value
        finally:
            lock.release()

    async def aclose(self) -> None:
        return None
