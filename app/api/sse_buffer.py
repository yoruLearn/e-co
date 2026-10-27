"""SSE 帧缓冲（Spec §15「断线 30s 内 session_id + last_event_id 续传，不重放已发送事件」）。

- 每帧分配**会话级递增 id**（Redis ``INCR sse:seq:{sid}``，跨副本唯一）；
- 帧写入 ``sse:{sid}`` list（cap 500、TTL 300s），按 id 过滤回放；
- Redis 不可用时降级为**进程内**缓冲（多副本不共享，但单副本仍可续传）。

只缓存已发出的帧——回放天然"不重放已发送事件"：客户端带来的 ``Last-Event-ID`` 之后才有帧。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from redis.asyncio import Redis

from app.api.sse import format_event
from app.common.logging import get_logger, log_warning

logger = get_logger("app.api.sse_buffer")


async def _maybe_await(value: Any) -> Any:
    """redis-py 部分方法返回 ``Awaitable[T] | T``（同步/异步双模），统一 await 安全。"""
    if hasattr(value, "__await__"):
        return await value
    return value


SEQ_PREFIX = "sse:seq:"
FRAMES_PREFIX = "sse:frames:"
DEFAULT_TTL_SECONDS = 300
DEFAULT_MAX_FRAMES = 500


@dataclass(frozen=True, slots=True)
class SseFrame:
    id: int
    event: str
    payload: dict[str, Any]

    def to_bytes(self) -> bytes:
        return (f"id: {self.id}\n" + format_event(self.event, self.payload)).encode()

    def to_json(self) -> str:
        return json.dumps(
            {"id": self.id, "event": self.event, "payload": self.payload},
            ensure_ascii=False,
        )

    @classmethod
    def from_json(cls, raw: str) -> SseFrame | None:
        try:
            payload: dict[str, Any] = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return None
        if not isinstance(payload.get("payload"), dict):
            return None
        return cls(
            id=int(payload.get("id", 0)),
            event=str(payload.get("event", "status")),
            payload=payload["payload"],
        )


class SseBuffer:
    """会话级帧缓冲；``redis=None`` 时退化为进程内。"""

    def __init__(
        self,
        redis: Redis | None,
        *,
        ttl_seconds: int = DEFAULT_TTL_SECONDS,
        max_frames: int = DEFAULT_MAX_FRAMES,
    ) -> None:
        self._redis = redis
        self._ttl = max(30, ttl_seconds)
        self._max = max(10, max_frames)
        self._local_seq: dict[str, int] = {}
        self._local_frames: dict[str, list[SseFrame]] = {}

    @staticmethod
    def _seq_key(session_id: str) -> str:
        return f"{SEQ_PREFIX}{session_id}"

    @staticmethod
    def _frames_key(session_id: str) -> str:
        return f"{FRAMES_PREFIX}{session_id}"

    async def next_id(self, session_id: str) -> int:
        if self._redis is not None:
            try:
                value = await _maybe_await(self._redis.incr(self._seq_key(session_id)))
                await _maybe_await(self._redis.expire(self._seq_key(session_id), self._ttl))
                return int(value)
            except Exception as exc:
                log_warning(logger, "sse.seq.failed", reason=type(exc).__name__)
        self._local_seq[session_id] = self._local_seq.get(session_id, 0) + 1
        return self._local_seq[session_id]

    async def append(self, session_id: str, frame: SseFrame) -> None:
        if self._redis is not None:
            try:
                key = self._frames_key(session_id)
                await _maybe_await(self._redis.rpush(key, frame.to_json()))
                await _maybe_await(self._redis.ltrim(key, -self._max, -1))
                await _maybe_await(self._redis.expire(key, self._ttl))
                return
            except Exception as exc:
                log_warning(logger, "sse.append.failed", reason=type(exc).__name__)
        frames = self._local_frames.setdefault(session_id, [])
        frames.append(frame)
        if len(frames) > self._max:
            del frames[: len(frames) - self._max]

    async def since(self, session_id: str, last_id: int) -> list[SseFrame]:
        if self._redis is not None:
            try:
                raw_frames = await _maybe_await(
                    self._redis.lrange(self._frames_key(session_id), 0, -1)
                )
                frames = [
                    frame
                    for frame in (SseFrame.from_json(item) for item in raw_frames)
                    if frame is not None
                ]
                return [frame for frame in frames if frame.id > last_id]
            except Exception as exc:
                log_warning(logger, "sse.since.failed", reason=type(exc).__name__)
        return [frame for frame in self._local_frames.get(session_id, []) if frame.id > last_id]

    async def clear(self, session_id: str) -> None:
        self._local_seq.pop(session_id, None)
        self._local_frames.pop(session_id, None)
        if self._redis is None:
            return
        try:
            await _maybe_await(
                self._redis.delete(self._seq_key(session_id), self._frames_key(session_id))
            )
        except Exception as exc:
            log_warning(logger, "sse.clear.failed", reason=type(exc).__name__)
