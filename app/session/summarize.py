"""会话摘要压缩（Spec §7 / FR-CHAT-07「超过 10 轮自动摘要」）。

触发条件：轮数超过窗口的 2 倍 + 2（避免每轮都调模型）。
把"最老的一段"压成不超过 ~600 字的摘要，保留订单号/商品/诉求/已确认结论，
再与旧摘要拼接；失败时只截断、不丢对话。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from app.common.logging import get_logger, log_warning
from app.llm.base import ChatModel
from app.llm.types import ChatMessage, LLMError
from app.prompt.guard import with_system_guard
from app.prompt.registry import PromptRegistry

logger = get_logger("app.session.summarize")

Summarizer = Callable[[str], Awaitable[str]]
MAX_SUMMARY_CHARS = 600


class SessionSummarizer:
    def __init__(self, chat: ChatModel, prompts: PromptRegistry) -> None:
        self._chat = chat
        self._prompts = prompts

    async def __call__(self, transcript: str) -> str:
        if not transcript.strip():
            return ""
        try:
            rendered = await self._prompts.render("summary", history=transcript)
            result = await self._chat.complete(
                [
                    ChatMessage(
                        role="system",
                        content=with_system_guard("你是会话摘要器，只保留售后相关事实。"),
                    ),
                    ChatMessage(role="user", content=rendered),
                ],
                temperature=0.0,
                max_tokens=320,
                tag="summary",
            )
        except (LLMError, KeyError) as exc:
            log_warning(logger, "summary.failed", reason=type(exc).__name__)
            return ""
        return result.content.strip()[:MAX_SUMMARY_CHARS]


def transcript_of(turns: list[dict[str, str]]) -> str:
    return "\n".join(f"{item.get('role', 'user')}: {item.get('content', '')}" for item in turns)
