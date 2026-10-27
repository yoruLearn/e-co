"""Chroma REST 客户端测试（MockTransport），不依赖真实容器。"""

from __future__ import annotations

import json

import httpx
import pytest

from app.llm.embeddings import hash_embed
from app.rag.chroma import ChromaVectorIndex, InMemoryVectorIndex
from app.rag.errors import RetrievalError


def make_index(handler: object) -> ChromaVectorIndex:
    return ChromaVectorIndex(
        "http://chroma:8000",
        transport=httpx.MockTransport(handler),  # type: ignore[arg-type]
    )


async def test_create_collection_uses_get_or_create() -> None:
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, json={"id": "col-1", "name": "aftersales_v1"})

    index = make_index(handler)
    collection_id = await index.create_collection("aftersales_v1")
    assert collection_id == "col-1"
    body = json.loads(captured[0].content)
    assert str(captured[0].url) == "http://chroma:8000/api/v1/collections"
    assert body["name"] == "aftersales_v1"
    assert body["get_or_create"] is True
    assert body["metadata"] == {"hnsw:space": "cosine"}


async def test_create_collection_without_id_fails() -> None:
    index = make_index(lambda _request: httpx.Response(200, json={"name": "x"}))
    with pytest.raises(RetrievalError, match="collection id"):
        await index.create_collection("x")


async def test_upsert_posts_vectors_and_metadata() -> None:
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, json={})

    index = make_index(handler)
    await index.upsert(
        "col-1",
        ids=["d1#0"],
        embeddings=[[0.1, 0.2]],
        documents=["正文"],
        metadatas=[{"doc_id": "d1", "title": "政策", "section": "退款"}],
    )
    body = json.loads(captured[0].content)
    assert str(captured[0].url).endswith("/collections/col-1/upsert")
    assert body["ids"] == ["d1#0"]
    assert body["documents"] == ["正文"]
    assert body["metadatas"][0]["section"] == "退款"


async def test_upsert_skips_empty_batch() -> None:
    calls: list[int] = []

    def handler(_request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(200, json={})

    index = make_index(handler)
    await index.upsert("col-1", ids=[], embeddings=[], documents=[], metadatas=[])
    assert calls == []


async def test_query_maps_cosine_distance_to_score() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "ids": [["d1#0", "d2#0"]],
                "documents": [["七天无理由退货说明", "运费规则"]],
                "metadatas": [
                    [{"doc_id": "d1", "title": "售后政策", "section": "七天无理由"}],
                    [{"doc_id": "d2", "title": "售后政策", "section": "运费"}],
                ],
                "distances": [[0.1, 0.6]],
            },
        )

    index = make_index(handler)
    hits = await index.search("col-1", [0.1, 0.2], top_k=2)
    assert [hit.chunk_id for hit in hits] == ["d1#0", "d2#0"]
    assert hits[0].score == pytest.approx(0.9)
    assert hits[1].score == pytest.approx(0.4)
    assert hits[0].section == "七天无理由"


async def test_query_empty_result() -> None:
    index = make_index(lambda _request: httpx.Response(200, json={"ids": [[]], "documents": [[]]}))
    assert await index.search("col-1", [0.0], top_k=3) == []


async def test_http_error_maps_to_retrieval_error() -> None:
    index = make_index(lambda _request: httpx.Response(500, text="boom", json={"error": "boom"}))
    with pytest.raises(RetrievalError) as excinfo:
        await index.search("col-1", [0.0], top_k=3)
    assert str(excinfo.value.code) == "AS-5032"
    assert excinfo.value.detail["path"].endswith("/query")


async def test_connection_error_maps_to_retrieval_error() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    index = make_index(handler)
    with pytest.raises(RetrievalError, match="不可达"):
        await index.create_collection("x")


async def test_heartbeat_true_and_false() -> None:
    ok = make_index(lambda _request: httpx.Response(200, json={"nanosecond heartbeat": 1}))
    assert await ok.heartbeat() is True

    def failing(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down")

    assert await make_index(failing).heartbeat() is False


async def test_delete_collection_tolerates_404() -> None:
    index = make_index(lambda _request: httpx.Response(404, json={"error": "not found"}))
    await index.delete_collection("aftersales_v1")


async def test_inmemory_index_orders_by_cosine() -> None:
    index = InMemoryVectorIndex()
    collection_id = await index.create_collection("aftersales_v1")
    documents = ["退款多久到账", "退款到账时间是多久", "物流单号在哪里查"]
    await index.upsert(
        collection_id,
        ids=["d1#0", "d2#0", "d3#0"],
        embeddings=[hash_embed(text, 256) for text in documents],
        documents=documents,
        metadatas=[
            {"doc_id": "d1", "title": "退款", "section": "到账"},
            {"doc_id": "d2", "title": "退款", "section": "时效"},
            {"doc_id": "d3", "title": "物流", "section": "查询"},
        ],
    )
    hits = await index.search(collection_id, hash_embed("退款到账", 256), top_k=3)
    assert len(hits) == 3
    assert hits[0].section == "到账"
    assert hits[-1].section == "查询"
