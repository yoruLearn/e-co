"""资料可答性判定（Spec §7.2 三档判定的补强）。

问题：向量/BM25 对"看起来很售后"的越界问题（海外仓维修、延保购买、上门安装…）
仍会给 0.55-0.65 的分数，落入"带保留作答"档，模型就会拿不相干政策硬答。
字符覆盖率阈值实测不可用（域内/域外分布重叠），所以这里用**一次结构化判定**：

- 只在 ``band=answer_with_caveat`` 且 top1 < 阈值（默认 0.65）时触发，成本可控；
- 判定失败（模型/网络异常）→ **fail-open（按可答处理）**，不让判定器成为新故障点；
- 判定结果写进 ``degraded``，评测与日志都能归因。
"""

from __future__ import annotations

import json
from typing import Protocol

from app.common.logging import get_logger, log_warning
from app.llm.base import ChatModel
from app.llm.types import ChatMessage, LLMError
from app.prompt.guard import with_system_guard, wrap_user_input
from app.prompt.registry import PromptRegistry

logger = get_logger("app.rag.answerability")

MAX_PASSAGE_CHARS = 3200


class AnswerabilityChecker(Protocol):
    async def is_answerable(self, *, question: str, passages: list[str]) -> bool | None:
        """True=可答；False=资料未覆盖；None=判定失败（调用方按可答处理）。"""
        ...


class AlwaysAnswerableChecker:
    async def is_answerable(self, *, question: str, passages: list[str]) -> bool | None:
        return True


class LlmAnswerabilityChecker:
    def __init__(self, chat: ChatModel, prompts: PromptRegistry) -> None:
        self._chat = chat
        self._prompts = prompts

    async def is_answerable(self, *, question: str, passages: list[str]) -> bool | None:
        if not passages:
            return False
        joined = "\n\n".join(passages)[:MAX_PASSAGE_CHARS]
        try:
            rendered = await self._prompts.render(
                "answerability",
                question=wrap_user_input(question),
                passages=joined,
            )
            result = await self._chat.complete(
                [
                    ChatMessage(
                        role="system",
                        content=with_system_guard("你是资料可答性判定器。"),
                    ),
                    ChatMessage(role="user", content=rendered),
                ],
                temperature=0.0,
                max_tokens=120,
                json_mode=True,
                tag="answerability",
            )
            payload = json.loads(result.content)
        except (LLMError, KeyError, json.JSONDecodeError, ValueError) as exc:
            log_warning(logger, "answerability.failed", reason=type(exc).__name__)
            return None
        value = payload.get("answerable")
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            return value.strip().lower() in {"true", "yes", "1", "是"}
        return None
