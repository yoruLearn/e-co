"""用户级限流与会话互斥（Spec §15 / FR-CHAT-08）。

- **限流**：Redis ZSET 滑动窗口，默认 20 次/分钟/用户；超限 429 `AS-4290`；
- **会话互斥**：同一 ``session_id`` 同时只允许一个流在跑，避免多标签页
  把同一会话的 checkpoint 写乱；拿不到锁直接 429（带 retry_after）；
- **降级**：Redis 不可用时**放行**（限流是保护措施，不是正确性依赖），只打告警。

为什么不用固定窗口：固定窗口在边界会放过 2 倍流量；ZSET 滑动窗口实现简单、误差小。
"""

from __future__ import annotations

import time
from typing import Any
from uuid import uuid4

from redis.asyncio import Redis

from app.common.logging import get_logger, log_warning

logger = get_logger("app.services.ratelimit")


async def _maybe_await(value: Any) -> Any:
    if hasattr(value, "__await__"):
        return await value
    return value


class RateLimiter:
    def __init__(
        self,
        redis: Redis | None,
        *,
        limit_per_min: int = 20,
        window_seconds: int = 60,
        session_lock_ttl_seconds: int = 60,
    ) -> None:
        self._redis = redis
        self._limit = max(1, limit_per_min)
        self._window = max(1, window_seconds)
        self._lock_ttl = max(10, session_lock_ttl_seconds)

    @staticmethod
    def _rate_key(user_id: str) -> str:
        return f"rl:{user_id}"

    @staticmethod
    def _lock_key(session_id: str) -> str:
        return f"lock:chat:{session_id}"

    async def allow(self, user_id: str) -> tuple[bool, int]:
        """返回 ``(是否放行, 建议重试秒数)``。"""
        if self._redis is None:
            return True, 0
        key = self._rate_key(user_id)
        now = time.time()
        try:
            await _maybe_await(self._redis.zremrangebyscore(key, 0, now - self._window))
            await _maybe_await(self._redis.zadd(key, {f"{now:.6f}:{uuid4().hex[:8]}": now}))
            count = int(await _maybe_await(self._redis.zcard(key)))
            await _maybe_await(self._redis.expire(key, self._window))
        except Exception as exc:  # Redis 故障 → 放行（保护性措施，不阻断业务）
            log_warning(logger, "ratelimit.failed", reason=type(exc).__name__)
            return True, 0
        if count > self._limit:
            return False, self._window
        return True, 0

    async def acquire_session(self, session_id: str) -> bool:
        if self._redis is None:
            return True
        try:
            ok = await _maybe_await(
                self._redis.set(self._lock_key(session_id), "1", nx=True, ex=self._lock_ttl)
            )
        except Exception as exc:
            log_warning(logger, "session.lock.failed", reason=type(exc).__name__)
            return True
        return bool(ok)

    async def release_session(self, session_id: str) -> None:
        if self._redis is None:
            return
        try:
            await _maybe_await(self._redis.delete(self._lock_key(session_id)))
        except Exception as exc:
            log_warning(logger, "session.unlock.failed", reason=type(exc).__name__)
