"""LangGraph checkpointer（Spec §6.2 第 5 条 / FR-AS-06）。

- 生产：``AsyncRedisSaver``（会话级 thread_id，TTL 24h），支撑审批挂起跨进程恢复；
- 测试/无 Redis：``InMemorySaver``，图机制一致、重启即丢（可接受）。

M1-00 spike 实测（langgraph 1.2.18 / langgraph-checkpoint-redis 0.5.2）：

- 签名：``AsyncRedisSaver(redis_url=..., ttl={"default_ttl": <分钟>, "refresh_on_read": True})``；
- ``asetup()`` 为异步，负责建索引，必须在首次使用前 await；
- 构造不会立即连 Redis（连接延迟到 asetup/使用时），因此应用启动不被阻塞。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.memory import InMemorySaver

from app.common.config import Settings
from app.common.logging import get_logger, log_event

logger = get_logger("app.agent.checkpointer")


@dataclass(frozen=True, slots=True)
class CheckpointPolicy:
    backend: str
    ttl_hours: int

    @classmethod
    def from_settings(cls, settings: Settings) -> CheckpointPolicy:
        return cls(backend=settings.checkpointer_backend, ttl_hours=settings.session_ttl_hours)

    @property
    def ttl_minutes(self) -> int:
        return max(1, self.ttl_hours * 60)

    @property
    def ttl_seconds(self) -> int:
        return self.ttl_minutes * 60


def build_checkpointer(settings: Settings) -> BaseCheckpointSaver[Any]:
    """按配置构造 checkpointer（不会发起网络连接）。"""
    policy = CheckpointPolicy.from_settings(settings)
    if policy.backend == "memory":
        log_event(logger, "checkpoint.backend", backend="memory")
        return InMemorySaver()
    try:
        from langgraph.checkpoint.redis.aio import AsyncRedisSaver
    except ImportError as exc:  # pragma: no cover - 依赖缺失时 fail-fast
        msg = "CHECKPOINTER_BACKEND=redis 但未安装 langgraph-checkpoint-redis"
        raise RuntimeError(msg) from exc
    log_event(logger, "checkpoint.backend", backend="redis", ttl_minutes=policy.ttl_minutes)
    return AsyncRedisSaver(
        redis_url=settings.redis_url,
        ttl={"default_ttl": policy.ttl_minutes, "refresh_on_read": True},
    )


async def init_checkpointer(saver: BaseCheckpointSaver[Any]) -> None:
    """Redis 后端需要建索引（``asetup``）；内存后端是 no-op。"""
    setup = getattr(saver, "asetup", None)
    if setup is not None:
        await setup()
        log_event(logger, "checkpoint.setup", backend=type(saver).__name__)


async def close_checkpointer(saver: BaseCheckpointSaver[Any]) -> None:
    """释放 Redis 连接（不同版本方法名不同，逐个探测）。"""
    for method_name in ("aclose", "close"):
        method = getattr(saver, method_name, None)
        if method is None:
            continue
        result = method()
        if hasattr(result, "__await__"):
            await result
        return
