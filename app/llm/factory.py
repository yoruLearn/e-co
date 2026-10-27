"""Provider 工厂：按配置组装 ChatModel / EmbeddingModel（M1）。"""

from __future__ import annotations

from pathlib import Path

from app.common.config import Settings
from app.common.logging import get_logger
from app.llm.base import ChatModel, EmbeddingModel
from app.llm.embeddings import ApiEmbedding, FastEmbedEmbedding, HashEmbedding
from app.llm.mock import MockChatModel
from app.llm.openai import OpenAICompatibleChatModel

logger = get_logger("app.llm.factory")


def build_chat_model(settings: Settings, *, cassette_dir: Path | str | None = None) -> ChatModel:
    """``LLM_PROVIDER`` 决定实现；mock 只能回放已录制请求。"""
    if settings.llm_provider == "mock":
        directory = Path(cassette_dir or settings.llm_mock_cassette_dir)
        mock_model = MockChatModel.from_dir(directory)
        logger.info("llm.provider", extra={"provider": "mock", "cassette_dir": str(directory)})
        return mock_model
    real_model = OpenAICompatibleChatModel(settings)
    logger.info(
        "llm.provider",
        extra={
            "provider": "openai",
            "model": real_model.name,
            "fallback": settings.llm_fallback_model,
            "base_url": settings.llm_base_url,
        },
    )
    return real_model


def build_embedding(settings: Settings) -> EmbeddingModel:
    """``EMBEDDING_PROVIDER`` 决定实现；hash 仅用于离线/单测。"""
    if settings.embedding_provider == "fastembed":
        return FastEmbedEmbedding(
            model_name=settings.embedding_model,
            dim=settings.embedding_dim,
            hf_endpoint=settings.hf_endpoint,
        )
    if settings.embedding_provider == "api":
        return ApiEmbedding(
            url=settings.embedding_api_url,
            model=settings.embedding_model,
            dim=settings.embedding_dim,
            api_key=settings.llm_api_key(),
        )
    return HashEmbedding(dim=settings.embedding_dim)
