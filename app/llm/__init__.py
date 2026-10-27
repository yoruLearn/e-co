"""LLM 能力层：协议、OpenAI 兼容实现、mock 回放、embedding。"""

from app.llm.base import ChatModel, EmbeddingModel
from app.llm.factory import build_chat_model, build_embedding
from app.llm.types import ChatDelta, ChatMessage, ChatResult, LLMError, Usage

__all__ = [
    "ChatDelta",
    "ChatMessage",
    "ChatModel",
    "ChatResult",
    "EmbeddingModel",
    "LLMError",
    "Usage",
    "build_chat_model",
    "build_embedding",
]
