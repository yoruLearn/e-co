"""Embedding 测试：hash 确定性、API 解析、工厂选择；本地模型用 local_embed 标记单独跑。"""

from __future__ import annotations

import json
import os

import httpx
import pytest

from app.common.config import Settings
from app.llm.embeddings import (
    ApiEmbedding,
    FastEmbedEmbedding,
    HashEmbedding,
    cosine,
    features_of,
    hash_embed,
    normalize_text,
)
from app.llm.factory import build_chat_model, build_embedding
from app.llm.mock import MockChatModel
from app.llm.types import LLMError


def test_normalize_text_removes_punctuation_and_case() -> None:
    assert normalize_text("  退款多久到账？ ") == "退款多久到账"
    assert normalize_text("Refund  Status!") == "refund status"
    assert normalize_text("") == ""


def test_features_cover_chinese_unigram_and_bigram() -> None:
    features = features_of("退款到账")
    assert "c:退" in features
    assert "g2:退款" in features
    assert "g3:退款到" in features


def test_hash_embedding_is_deterministic_and_normalized() -> None:
    first = hash_embed("七天无理由退货", 64)
    second = hash_embed("七天无理由退货", 64)
    assert first == second
    assert len(first) == 64
    assert cosine(first, first) == pytest.approx(1.0)
    assert cosine(first, hash_embed("七天无理由退货！", 64)) == pytest.approx(1.0)


def test_hash_embedding_prefers_lexically_related_text() -> None:
    query = hash_embed("退款多久到账", 512)
    related = hash_embed("退款到账时间是多久", 512)
    unrelated = hash_embed("物流单号在哪里查", 512)
    assert cosine(query, related) > cosine(query, unrelated)


def test_hash_embedding_empty_text_is_zero_vector() -> None:
    vector = hash_embed("", 16)
    assert vector == [0.0] * 16
    assert cosine(vector, hash_embed("退款", 16)) == 0.0


def test_cosine_rejects_dimension_mismatch() -> None:
    with pytest.raises(ValueError, match="维度"):
        cosine([1.0, 0.0], [1.0])


async def test_hash_embedding_model_batch() -> None:
    model = HashEmbedding(dim=32)
    vectors = await model.embed(["退款", "换货"])
    assert len(vectors) == 2
    assert all(len(vector) == 32 for vector in vectors)
    assert model.dim == 32


async def test_api_embedding_parses_response_and_updates_dim() -> None:
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(
            200,
            json={"data": [{"embedding": [0.1, 0.2]}, {"embedding": [0.3, 0.4]}]},
        )

    model = ApiEmbedding(
        url="https://embed.example/v1",
        model="bge",
        dim=8,
        api_key="sk-x",
        transport=httpx.MockTransport(handler),
    )
    vectors = await model.embed(["a", "b"])
    assert str(captured[0].url) == "https://embed.example/v1/embeddings"
    assert json.loads(captured[0].content) == {"model": "bge", "input": ["a", "b"]}
    assert vectors == [[0.1, 0.2], [0.3, 0.4]]
    assert model.dim == 2


async def test_api_embedding_error_status() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"error": "down"})

    model = ApiEmbedding(
        url="https://embed.example/v1/embeddings",
        model="bge",
        dim=8,
        transport=httpx.MockTransport(handler),
    )
    with pytest.raises(LLMError) as excinfo:
        await model.embed(["a"])
    assert excinfo.value.status == 500


def test_api_embedding_requires_url() -> None:
    with pytest.raises(LLMError, match="EMBEDDING_API_URL"):
        ApiEmbedding(url=None, model="bge", dim=8)


def test_factory_defaults_to_offline_safe_implementations() -> None:
    settings = Settings(_env_file=None, app_env="test")
    assert isinstance(build_embedding(settings), HashEmbedding)
    assert isinstance(build_chat_model(settings, cassette_dir="evals/cassettes"), MockChatModel)


@pytest.mark.local_embed
@pytest.mark.skipif(
    os.environ.get("RUN_LOCAL_EMBED") != "1",
    reason="需要下载本地模型；RUN_LOCAL_EMBED=1 时运行",
)
async def test_fastembed_semantic_vectors() -> None:
    pytest.importorskip("fastembed")
    settings = Settings(
        _env_file=None,
        app_env="test",
        embedding_provider="fastembed",
        embedding_model="BAAI/bge-small-zh-v1.5",
        embedding_dim=512,
        hf_endpoint="https://hf-mirror.com",
    )
    model = FastEmbedEmbedding(
        model_name=settings.embedding_model,
        dim=settings.embedding_dim,
        hf_endpoint=settings.hf_endpoint,
    )
    vectors = await model.embed(["退款多久到账", "退款到账时间是多久", "物流单号在哪里查"])
    query, related, unrelated = vectors
    assert len(query) == 512
    assert cosine(query, related) > cosine(query, unrelated)
