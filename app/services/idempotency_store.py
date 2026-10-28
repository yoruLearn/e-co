"""幂等存储（Spec §9.1 / §11.2）。

DB 唯一键是最终权威；Redis 只是快路径与"首次响应缓存"：

1. 首次请求：``SET NX`` 占位（pending）→ 执行业务 → 写回 done+response；
2. 并发重复：拿到 pending 的实例短暂轮询，等首个请求写回后重放；
3. 参数不一致：``AS-4004``；
4. Redis 不可用：返回 ``UNKNOWN``，调用方直接走 DB（唯一键兜底），不阻断业务。
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from redis.asyncio import Redis

from app.common.logging import get_logger, log_warning

logger = get_logger("app.services.idempotency")

IDEM_PREFIX = "idem:"
IDEM_TTL_SECONDS = 86_400
POLL_INTERVAL_S = 0.05
POLL_ATTEMPTS = 10


async def _maybe_await(value: Any) -> Any:
    if hasattr(value, "__await__"):
        return await value
    return value


class StoreDecision(StrEnum):
    NEW = "new"
    REPLAY = "replay"
    PENDING = "pending"
    CONFLICT = "conflict"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class StoreResult:
    decision: StoreDecision
    response: dict[str, Any] | None = None


class RedisIdempotencyStore:
    def __init__(self, redis: Redis | None, *, ttl_seconds: int = IDEM_TTL_SECONDS) -> None:
        self._redis = redis
        self._ttl = ttl_seconds

    @staticmethod
    def _key(key: str) -> str:
        return f"{IDEM_PREFIX}{key}"

    async def begin(self, key: str, params_hash: str) -> StoreResult:
        if self._redis is None:
            return StoreResult(StoreDecision.UNKNOWN)
        redis_key = self._key(key)
        try:
            created = await _maybe_await(
                self._redis.set(
                    redis_key,
                    json.dumps({"params_hash": params_hash, "response": None}),
                    nx=True,
                    ex=self._ttl,
                )
            )
            if created:
                return StoreResult(StoreDecision.NEW)
            for _ in range(POLL_ATTEMPTS):
                raw = await _maybe_await(self._redis.get(redis_key))
                if raw:
                    record = json.loads(raw)
                    if record.get("params_hash") != params_hash:
                        return StoreResult(StoreDecision.CONFLICT)
                    if record.get("response") is not None:
                        return StoreResult(StoreDecision.REPLAY, dict(record["response"]))
                await asyncio.sleep(POLL_INTERVAL_S)
            return StoreResult(StoreDecision.PENDING)
        except Exception as exc:
            log_warning(logger, "idem.begin.failed", reason=type(exc).__name__)
            return StoreResult(StoreDecision.UNKNOWN)

    async def complete(self, key: str, response: dict[str, Any]) -> None:
        if self._redis is None:
            return
        try:
            raw = await _maybe_await(self._redis.get(self._key(key)))
            record = json.loads(raw) if raw else {}
            record["response"] = response
            await _maybe_await(
                self._redis.set(
                    self._key(key), json.dumps(record, ensure_ascii=False), ex=self._ttl
                )
            )
        except Exception as exc:
            log_warning(logger, "idem.complete.failed", reason=type(exc).__name__)

    async def release(self, key: str) -> None:
        if self._redis is None:
            return
        try:
            await _maybe_await(self._redis.delete(self._key(key)))
        except Exception as exc:
            log_warning(logger, "idem.release.failed", reason=type(exc).__name__)
