"""会话记忆存储（Spec §7 / FR-CHAT-07 的存储层）。

Redis ``sess:{session_id}`` 存 ``{"turns": [...], "summary": "..."}``：

- 滑动窗口：只保留最近 ``SESSION_WINDOW_TURNS`` 轮（user+assistant 计一轮）；
- 超长压缩（摘要）在 M1-08 实现，这里预留 ``summary`` 字段；
- Redis 不可用时 **降级为无记忆**（只打警告，不让对话失败）。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from redis.asyncio import Redis

from app.common.logging import get_logger, log_warning

logger = get_logger("app.session.store")

MAX_TURNS_HARD_LIMIT = 200


@dataclass(slots=True)
class SessionState:
    turns: list[dict[str, str]] = field(default_factory=list)
    summary: str = ""

    def history(self) -> list[dict[str, str]]:
        return list(self.turns)

    def trimmed(self, *, window_turns: int) -> SessionState:
        """只保留最近 ``window_turns`` 轮（一条 user + 一条 assistant 记一轮）。"""
        limit = max(2, window_turns * 2)
        return SessionState(turns=self.turns[-limit:], summary=self.summary)


class SessionStore:
    def __init__(
        self,
        redis: Redis,
        *,
        ttl_seconds: int = 86_400,
        window_turns: int = 10,
    ) -> None:
        self._redis = redis
        self._ttl = ttl_seconds
        self._window_turns = window_turns

    @staticmethod
    def key(session_id: str) -> str:
        return f"sess:{session_id}"

    async def load(self, session_id: str) -> SessionState:
        try:
            raw = await self._redis.get(self.key(session_id))
        except Exception as exc:
            log_warning(logger, "session.load.failed", reason=type(exc).__name__)
            return SessionState()
        if not raw:
            return SessionState()
        try:
            payload: dict[str, Any] = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            log_warning(logger, "session.decode.failed", session_id=session_id)
            return SessionState()
        turns = payload.get("turns") or []
        cleaned = [
            {"role": str(item.get("role", "")), "content": str(item.get("content", ""))}
            for item in turns
            if isinstance(item, dict)
        ]
        return SessionState(
            turns=cleaned[-MAX_TURNS_HARD_LIMIT:], summary=str(payload.get("summary", ""))
        )

    async def append(
        self, session_id: str, *, user_message: str, assistant_message: str
    ) -> SessionState:
        state = await self.load(session_id)
        state.turns.extend(
            [
                {"role": "user", "content": user_message},
                {"role": "assistant", "content": assistant_message},
            ]
        )
        state = state.trimmed(window_turns=self._window_turns)
        try:
            await self._redis.set(
                self.key(session_id),
                json.dumps({"turns": state.turns, "summary": state.summary}, ensure_ascii=False),
                ex=self._ttl,
            )
        except Exception as exc:
            log_warning(logger, "session.save.failed", reason=type(exc).__name__)
        return state

    async def clear(self, session_id: str) -> None:
        try:
            await self._redis.delete(self.key(session_id))
        except Exception as exc:
            log_warning(logger, "session.clear.failed", reason=type(exc).__name__)
