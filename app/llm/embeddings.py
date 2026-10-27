"""Embedding 三实现（Spec Q2 选型未定 → 配置化）。

| 实现 | 用途 | 依赖 |
| --- | --- | --- |
| ``hash`` | CI/单测：确定性、完全离线，只保证"同文本同向量"，**不代表语义质量** | 无 |
| ``fastembed`` | 本地真实语义检索（bge-small-zh-v1.5，512 维，ONNX） | optional extra |
| ``api`` | 远程 embedding 服务（OpenAI 兼容） | httpx |

fastembed 首次运行会下载 ~100MB 模型；若 huggingface.co 不可达，用 ``HF_ENDPOINT``
指向镜像（本项目实测 ``https://hf-mirror.com`` 可用）。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import os
import re
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import httpx

from app.common.logging import get_logger
from app.llm.types import LLMError

logger = get_logger("app.llm.embedding")

_TOKEN_RE = re.compile(r"[a-z0-9]+|[\u4e00-\u9fff]")
_NON_WORD = re.compile(r"[^\w\u4e00-\u9fff]+", re.UNICODE)
_CJK = re.compile(r"[\u4e00-\u9fff]")


def normalize_text(text: str) -> str:
    """小写、去标点、折叠空白——保证 hash 向量对排版差异不敏感。"""
    lowered = text.lower().strip()
    cleaned = _NON_WORD.sub(" ", lowered)
    return re.sub(r"\s+", " ", cleaned).strip()


def features_of(text: str) -> list[str]:
    """特征：英文按词、中文按字符 bigram/trigram + 单字兜底。"""
    normalized = normalize_text(text)
    if not normalized:
        return []
    features: list[str] = _TOKEN_RE.findall(normalized)
    for token in _TOKEN_RE.findall(normalized):
        if not _CJK.match(token):
            continue
        # 中文：单字保留（短 query 只有 1-2 个字时 bigram 会消失）
        features.append(f"c:{token}")
    compact = normalized.replace(" ", "")
    for size in (2, 3):
        for index in range(len(compact) - size + 1):
            gram = compact[index : index + size]
            if any(_CJK.match(char) for char in gram):
                features.append(f"g{size}:{gram}")
    return features


def _hash_index(feature: str, dim: int) -> tuple[int, float]:
    digest = hashlib.blake2b(feature.encode("utf-8"), digest_size=8).digest()
    value = int.from_bytes(digest, "big")
    index = value % dim
    sign = 1.0 if (value >> 63) & 1 == 0 else -1.0
    return index, sign


def hash_embed(text: str, dim: int) -> list[float]:
    vector = [0.0] * dim
    for feature in features_of(text):
        index, sign = _hash_index(feature, dim)
        vector[index] += sign
    norm = math.sqrt(sum(value * value for value in vector))
    if norm == 0:
        return vector
    return [value / norm for value in vector]


def cosine(a: Sequence[float], b: Sequence[float]) -> float:
    if len(a) != len(b):
        msg = "向量维度不一致"
        raise ValueError(msg)
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


class HashEmbedding:
    name = "hash"

    def __init__(self, dim: int = 256) -> None:
        self._dim = dim

    @property
    def dim(self) -> int:
        return self._dim

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        return [hash_embed(text, self._dim) for text in texts]


class FastEmbedEmbedding:
    """本地 ONNX 语义向量；模型懒加载，避免应用启动时下载。"""

    name = "fastembed"

    def __init__(
        self,
        model_name: str = "BAAI/bge-small-zh-v1.5",
        *,
        dim: int = 512,
        cache_dir: str | Path | None = None,
        hf_endpoint: str | None = None,
    ) -> None:
        self._model_name = model_name
        self._dim = dim
        self._cache_dir = str(cache_dir) if cache_dir else None
        self._hf_endpoint = hf_endpoint
        self._model: Any = None
        self._lock = asyncio.Lock()

    @property
    def dim(self) -> int:
        return self._dim

    def _load(self) -> Any:
        if self._model is not None:
            return self._model
        if self._hf_endpoint:
            os.environ.setdefault("HF_ENDPOINT", self._hf_endpoint)
        try:
            from fastembed import TextEmbedding
        except ImportError as exc:  # pragma: no cover - 取决于安装的 extra
            raise LLMError(
                "EMBEDDING_PROVIDER=fastembed 但未安装 fastembed；"
                "请 `uv sync --extra local-embed` 或改回 hash",
                detail={"provider": "fastembed"},
            ) from exc
        logger.info(
            "embedding.load",
            extra={"model": self._model_name, "hf_endpoint": os.environ.get("HF_ENDPOINT")},
        )
        kwargs: dict[str, Any] = {"model_name": self._model_name}
        if self._cache_dir:
            kwargs["cache_dir"] = self._cache_dir
        self._model = TextEmbedding(**kwargs)
        return self._model

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        async with self._lock:
            model = await asyncio.to_thread(self._load)
            vectors = await asyncio.to_thread(lambda: list(model.embed(list(texts))))
        result = [[float(value) for value in vector] for vector in vectors]
        if result and len(result[0]) != self._dim:
            self._dim = len(result[0])
        return result


class ApiEmbedding:
    """OpenAI 兼容 ``/embeddings``。"""

    name = "api"

    def __init__(
        self,
        *,
        url: str | None,
        model: str,
        dim: int,
        api_key: str | None = None,
        client: httpx.AsyncClient | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout_s: float = 10.0,
    ) -> None:
        if not url:
            msg = "EMBEDDING_PROVIDER=api 时必须配置 EMBEDDING_API_URL"
            raise LLMError(msg, detail={"provider": "api"})
        self._url = url if url.endswith("/embeddings") else f"{url.rstrip('/')}/embeddings"
        self._model = model
        self._dim = dim
        self._api_key = api_key
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(timeout_s, connect=5.0), transport=transport
        )

    @property
    def dim(self) -> int:
        return self._dim

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        headers = {"Content-Type": "application/json"}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        try:
            response = await self._client.post(
                self._url,
                json={"model": self._model, "input": list(texts)},
                headers=headers,
            )
        except httpx.HTTPError as exc:
            raise LLMError(
                f"embedding 服务网络异常：{type(exc).__name__}", detail={"provider": "api"}
            ) from exc
        if response.status_code >= 400:
            raise LLMError(
                f"embedding 服务返回 {response.status_code}",
                status=response.status_code,
                detail={"provider": "api"},
            )
        try:
            body: dict[str, Any] = response.json()
        except (json.JSONDecodeError, ValueError) as exc:
            raise LLMError("embedding 响应非 JSON", detail={"provider": "api"}) from exc
        data = body.get("data") or []
        vectors: list[list[float]] = []
        for item in data:
            embedding = item.get("embedding") if isinstance(item, dict) else None
            if not isinstance(embedding, list):
                raise LLMError("embedding 响应缺少 embedding 字段", detail={"provider": "api"})
            vectors.append([float(value) for value in embedding])
        if vectors:
            self._dim = len(vectors[0])
        return vectors

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()
