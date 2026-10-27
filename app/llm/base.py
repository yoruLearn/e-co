"""Provider 协议（M1）。

``ChatModel`` / ``EmbeddingModel`` 是唯一允许调用模型的入口：
业务节点只依赖协议，因此 mock（录制回放）、真实网关、未来本地模型可以无缝替换。
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from typing import Protocol, runtime_checkable

from app.llm.types import ChatDelta, ChatMessage, ChatResult


@runtime_checkable
class ChatModel(Protocol):
    name: str

    async def complete(
        self,
        messages: Sequence[ChatMessage],
        *,
        temperature: float = 0.0,
        max_tokens: int | None = None,
        json_mode: bool = False,
        reasoning_effort: str | None = None,
        tag: str = "llm",
    ) -> ChatResult:
        """非流式补全（用于 router/rewrite/rerank 等结构化调用）。"""
        ...

    def stream(
        self,
        messages: Sequence[ChatMessage],
        *,
        temperature: float = 0.0,
        max_tokens: int | None = None,
        reasoning_effort: str | None = None,
        tag: str = "llm",
    ) -> AsyncIterator[ChatDelta]:
        """流式补全（用于 answer 节点）。"""
        ...

    async def aclose(self) -> None: ...


@runtime_checkable
class EmbeddingModel(Protocol):
    name: str

    @property
    def dim(self) -> int: ...

    async def embed(self, texts: Sequence[str]) -> list[list[float]]: ...
