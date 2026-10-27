"""Chroma 向量通道：**只用 REST**，不引入 chromadb 客户端。

理由：chromadb 客户端会拖入 onnxruntime/torch 系依赖（与本地 embedding 的 ONNX 冲突），
且版本漂移快；我们只需要 4 个端点，httpx 直连更可控，compose 里固定镜像版本即可。

端点（Chroma 0.5.x，``/api/v1``）：

- ``POST /collections``                        建/取集合（``get_or_create``）
- ``POST /collections/{id}/upsert``            写入向量 + 文档 + 元数据
- ``POST /collections/{id}/query``             查询（余弦距离 → 分数 ``1 - distance``）
- ``DELETE /collections/{name}``               删除（版本回滚后清理）
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

import httpx

from app.common.logging import get_logger, log_event
from app.rag.errors import RetrievalError

logger = get_logger("app.rag.chroma")


@dataclass(frozen=True, slots=True)
class VectorHit:
    chunk_id: str
    doc_id: str
    title: str
    section: str
    content: str
    score: float
    distance: float = 0.0


@runtime_checkable
class VectorIndex(Protocol):
    """向量通道协议；测试用内存实现替换 Chroma。"""

    async def create_collection(self, name: str) -> str: ...

    async def upsert(
        self,
        collection_id: str,
        *,
        ids: list[str],
        embeddings: list[list[float]],
        documents: list[str],
        metadatas: list[dict[str, str]],
    ) -> None: ...

    async def search(
        self, collection_id: str, embedding: list[float], *, top_k: int
    ) -> list[VectorHit]: ...

    async def delete_collection(self, name: str) -> None: ...


class ChromaVectorIndex:
    """Chroma HTTP 实现。"""

    def __init__(
        self,
        base_url: str,
        *,
        http: httpx.AsyncClient | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout_s: float = 10.0,
    ) -> None:
        self._base = base_url.rstrip("/")
        self._owns_client = http is None
        self._http = http or httpx.AsyncClient(
            timeout=httpx.Timeout(timeout_s, connect=3.0), transport=transport
        )

    # ------------------------------------------------------------------ #
    def _url(self, path: str) -> str:
        return f"{self._base}/api/v1{path}"

    async def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        try:
            response = await self._http.request(method, self._url(path), **kwargs)
        except httpx.HTTPError as exc:
            raise RetrievalError(
                f"Chroma 不可达：{type(exc).__name__}",
                detail={"base_url": self._base, "path": path},
            ) from exc
        if response.status_code >= 400:
            raise RetrievalError(
                f"Chroma 返回 {response.status_code}",
                detail={"path": path, "body": response.text[:200]},
            )
        return response

    # ------------------------------------------------------------------ #
    async def heartbeat(self) -> bool:
        try:
            response = await self._http.get(f"{self._base}/api/v1/heartbeat")
        except httpx.HTTPError:
            return False
        return response.status_code < 400

    async def create_collection(self, name: str) -> str:
        response = await self._request(
            "POST",
            "/collections",
            json={
                "name": name,
                "metadata": {"hnsw:space": "cosine"},
                "get_or_create": True,
            },
        )
        payload: dict[str, Any] = response.json()
        collection_id = payload.get("id")
        if not isinstance(collection_id, str) or not collection_id:
            raise RetrievalError("Chroma 未返回 collection id", detail={"name": name})
        log_event(logger, "chroma.collection", collection=name, collection_id=collection_id)
        return collection_id

    async def upsert(
        self,
        collection_id: str,
        *,
        ids: list[str],
        embeddings: list[list[float]],
        documents: list[str],
        metadatas: list[dict[str, str]],
    ) -> None:
        if not ids:
            return
        await self._request(
            "POST",
            f"/collections/{collection_id}/upsert",
            json={
                "ids": ids,
                "embeddings": embeddings,
                "documents": documents,
                "metadatas": metadatas,
            },
        )

    async def search(
        self, collection_id: str, embedding: list[float], *, top_k: int
    ) -> list[VectorHit]:
        response = await self._request(
            "POST",
            f"/collections/{collection_id}/query",
            json={
                "query_embeddings": [embedding],
                "n_results": top_k,
                "include": ["documents", "metadatas", "distances"],
            },
        )
        payload: dict[str, Any] = response.json()
        ids = _first_list(payload.get("ids"))
        documents = _first_list(payload.get("documents"))
        metadatas = _first_list(payload.get("metadatas"))
        distances = _first_list(payload.get("distances"))

        hits: list[VectorHit] = []
        for index, chunk_id in enumerate(ids):
            metadata = metadatas[index] if index < len(metadatas) else {}
            metadata = metadata if isinstance(metadata, dict) else {}
            document = documents[index] if index < len(documents) else ""
            distance = float(distances[index]) if index < len(distances) else 1.0
            hits.append(
                VectorHit(
                    chunk_id=str(chunk_id),
                    doc_id=str(metadata.get("doc_id", "")),
                    title=str(metadata.get("title", "")),
                    section=str(metadata.get("section", "")),
                    content=str(document or ""),
                    score=max(0.0, 1.0 - distance),
                    distance=distance,
                )
            )
        return hits

    async def delete_collection(self, name: str) -> None:
        try:
            response = await self._http.delete(self._url(f"/collections/{name}"))
        except httpx.HTTPError as exc:
            raise RetrievalError(
                f"Chroma 删除集合失败：{type(exc).__name__}", detail={"name": name}
            ) from exc
        if response.status_code not in (200, 202, 204, 404):
            raise RetrievalError(
                f"Chroma 删除集合返回 {response.status_code}", detail={"name": name}
            )

    async def aclose(self) -> None:
        if self._owns_client:
            await self._http.aclose()

    async def __aenter__(self) -> ChromaVectorIndex:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.aclose()


def _first_list(value: Any) -> list[Any]:
    if isinstance(value, list) and value and isinstance(value[0], list):
        return value[0]
    if isinstance(value, list):
        return value
    return []


class InMemoryVectorIndex:
    """测试/离线用：余弦相似度暴力检索。"""

    def __init__(self) -> None:
        self._collections: dict[str, dict[str, tuple[list[float], str, dict[str, str]]]] = {}

    async def create_collection(self, name: str) -> str:
        collection_id = f"mem-{name}"
        self._collections.setdefault(collection_id, {})
        return collection_id

    async def upsert(
        self,
        collection_id: str,
        *,
        ids: list[str],
        embeddings: list[list[float]],
        documents: list[str],
        metadatas: list[dict[str, str]],
    ) -> None:
        store = self._collections.setdefault(collection_id, {})
        for index, chunk_id in enumerate(ids):
            store[chunk_id] = (
                embeddings[index],
                documents[index],
                metadatas[index],
            )

    async def search(
        self, collection_id: str, embedding: list[float], *, top_k: int
    ) -> list[VectorHit]:
        from app.llm.embeddings import cosine

        store = self._collections.get(collection_id, {})
        scored = [
            (chunk_id, cosine(embedding, vector), document, metadata)
            for chunk_id, (vector, document, metadata) in store.items()
        ]
        scored.sort(key=lambda item: item[1], reverse=True)
        return [
            VectorHit(
                chunk_id=chunk_id,
                doc_id=str(metadata.get("doc_id", "")),
                title=str(metadata.get("title", "")),
                section=str(metadata.get("section", "")),
                content=document,
                score=max(0.0, score),
                distance=1.0 - score,
            )
            for chunk_id, score, document, metadata in scored[:top_k]
        ]

    async def delete_collection(self, name: str) -> None:
        self._collections.pop(f"mem-{name}", None)

    async def aclose(self) -> None:
        return None


async def wait_for_chroma(
    index: ChromaVectorIndex, *, timeout_s: float = 30.0, interval_s: float = 1.0
) -> bool:
    """容器刚起时 Chroma 需要几秒初始化；索引脚本用它等待而不是直接失败。"""
    deadline = asyncio.get_running_loop().time() + timeout_s
    while asyncio.get_running_loop().time() < deadline:
        if await index.heartbeat():
            return True
        await asyncio.sleep(interval_s)
    return False
