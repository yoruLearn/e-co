"""会话记忆存储（Spec §7 / FR-CHAT-07）。

Redis ``sess:{session_id}`` 存::

    {"turns": [...], "summary": "...", "archive": "...", "pending": 0}

三个机制配合，兼顾"不丢上下文"与"不每轮都调模型"：

1. **滑动窗口**：只保留最近 ``SESSION_WINDOW_TURNS`` 轮（user+assistant 记一轮）；
2. **原文归档**：被挤出窗口的轮次先原样追进 ``archive``（有上限），保证不丢信息；
3. **摘要压缩**：累计挤出 ≥2 轮才调一次 LLM 把 ``archive`` 压成 ``summary``，
   成功即清空 archive；失败保留原文，下一轮再试。

Redis 不可用时**降级为无记忆**（只打警告，不让对话失败）。
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from redis.asyncio import Redis

from app.common.logging import get_logger, log_warning
from app.session.summarize import transcript_of

logger = get_logger("app.session.store")

MAX_TURNS_HARD_LIMIT = 200
MAX_ARCHIVE_CHARS = 1500
MAX_SUMMARY_CHARS = 600
SUMMARY_EVERY_EXCESS_ROUNDS = 2


@dataclass(slots=True)
class SessionState:
    turns: list[dict[str, str]] = field(default_factory=list)
    summary: str = ""
    archive: str = ""
    pending: int = 0

    def history(self) -> list[dict[str, str]]:
        """给模型的历史：摘要（+未压缩归档）在前，最近几轮在后。"""
        prefix_parts: list[str] = []
        if self.summary:
            prefix_parts.append(f"（历史摘要）{self.summary}")
        if self.archive:
            prefix_parts.append(f"（更早的对话原文）{self.archive}")
        if not prefix_parts:
            return list(self.turns)
        return [
            {"role": "system", "content": "\n".join(prefix_parts)},
            *self.turns,
        ]


class SessionStore:
    def __init__(
        self,
        redis: Redis,
        *,
        ttl_seconds: int = 86_400,
        window_turns: int = 10,
        summarizer: Callable[[str], Awaitable[str]] | None = None,
    ) -> None:
        self._redis = redis
        self._ttl = ttl_seconds
        self._window_turns = window_turns
        self._summarizer = summarizer

    @staticmethod
    def key(session_id: str) -> str:
        return f"sess:{session_id}"

    async def load(self, session_id: str) -> SessionState:
        try:
            raw = await self._redis.get(self.key(session_id))
        except Exception as exc:  # 记忆失败不得阻断对话
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
            turns=cleaned[-MAX_TURNS_HARD_LIMIT:],
            summary=str(payload.get("summary", "")),
            archive=str(payload.get("archive", ""))[:MAX_ARCHIVE_CHARS],
            pending=int(payload.get("pending", 0) or 0),
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

        limit = max(2, self._window_turns * 2)
        if len(state.turns) > limit:
            excess = state.turns[: len(state.turns) - limit]
            state.turns = state.turns[len(state.turns) - limit :]
            state.archive = (state.archive + "\n" + transcript_of(excess)).strip()[
                -MAX_ARCHIVE_CHARS:
            ]
            state.pending += len(excess) // 2

        if (
            self._summarizer is not None
            and state.pending >= SUMMARY_EVERY_EXCESS_ROUNDS
            and state.archive
        ):
            try:
                compressed = await self._summarizer(state.archive)
            except Exception as exc:  # 摘要失败保留原文，下一轮再试
                log_warning(logger, "session.summary.failed", reason=type(exc).__name__)
                compressed = ""
            if compressed:
                state.summary = (state.summary + "\n" + compressed).strip()[-MAX_SUMMARY_CHARS:]
                state.archive = ""
                state.pending = 0

        try:
            await self._redis.set(
                self.key(session_id),
                json.dumps(
                    {
                        "turns": state.turns,
                        "summary": state.summary,
                        "archive": state.archive,
                        "pending": state.pending,
                    },
                    ensure_ascii=False,
                ),
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
