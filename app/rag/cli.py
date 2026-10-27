"""RAG CLI（Spec §7.1 / §7.2 的可操作入口）。

用法::

    # 离线检索（hash embedding + 内存索引，不依赖 MySQL/Chroma；用于验证管道与语料）
    python -m app.rag.cli query "退款多久到账" --offline

    # 在线检索（需要 MySQL + Chroma 已起，且索引已构建）
    python -m app.rag.cli query "退款多久到账" --version v1

    # 构建/重建索引（写入 Chroma + rag_chunk，并激活版本）
    python -m app.rag.cli rebuild --version v1

    # 查看索引版本
    python -m app.rag.cli versions

离线模式**不代表检索质量**（hash 向量只有词面相似度），它的用途是：CI/无依赖环境下
验证"语料 → 分块 → 召回 → 融合 → 阈值"这条链路本身。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any

from app.common.clients import AppClients, build_clients
from app.common.config import Settings, get_settings
from app.common.logging import configure_logging
from app.llm.factory import build_embedding
from app.rag.chroma import ChromaVectorIndex, InMemoryVectorIndex
from app.rag.chunker import collection_name, load_corpus
from app.rag.indexer import Indexer
from app.rag.keyword import (
    KeywordDocument,
    MemoryBM25KeywordIndex,
    MysqlKeywordIndex,
    chunk_id_of,
)
from app.rag.pipeline import HybridRetriever, RetrievalResult

EXIT_OK = 0
EXIT_ERROR = 1


def render_result(result: RetrievalResult, *, as_json: bool = False) -> str:
    """把检索结果渲染成可粘贴到验收记录的文本。"""
    if as_json:
        return json.dumps(
            {
                "query": result.query,
                "index_version": result.index_version,
                "band": str(result.band),
                "top1_score": round(result.top1_score, 4),
                "degraded": result.degraded,
                "hits": [
                    {
                        "rank": rank,
                        "chunk_id": hit.chunk_id,
                        "doc_id": hit.doc_id,
                        "title": hit.title,
                        "section": hit.section,
                        "score": round(hit.score, 4),
                        "vector_score": None
                        if hit.vector_score is None
                        else round(hit.vector_score, 4),
                        "keyword_score": None
                        if hit.keyword_score is None
                        else round(hit.keyword_score, 4),
                        "snippet": hit.content[:120],
                    }
                    for rank, hit in enumerate(result.hits, start=1)
                ],
            },
            ensure_ascii=False,
            indent=2,
        )
    lines = [
        f"query      : {result.query}",
        f"version    : {result.index_version}",
        f"band       : {result.band}  (top1={result.top1_score:.4f})",
        f"degraded   : {', '.join(result.degraded) or '-'}",
        "",
    ]
    for rank, hit in enumerate(result.hits, start=1):
        lines.append(
            f"[{rank}] {hit.score:.4f}  {hit.doc_id} / {hit.section}  "
            f"(vec={hit.vector_score}, kw={hit.keyword_score})"
        )
        lines.append(f"    {hit.content[:100]}")
    if not result.hits:
        lines.append("（无命中）")
    return "\n".join(lines)


def _offline_components(
    settings: Settings, corpus_dir: Path, version: str
) -> tuple[Any, InMemoryVectorIndex, MemoryBM25KeywordIndex, list[Any]]:
    """离线装配：真实配置的 embedding（hash/fastembed）+ 内存向量索引 + 内存 BM25。"""
    documents = load_corpus(corpus_dir, version=version)
    chunks = [chunk for document in documents for chunk in document.chunks]
    embedding = build_embedding(settings)
    vector_index = InMemoryVectorIndex()
    keyword_index = MemoryBM25KeywordIndex()
    keyword_index.add_documents(
        version,
        [
            KeywordDocument(
                chunk_id=chunk_id_of(chunk.doc_id, chunk.index),
                doc_id=chunk.doc_id,
                title=chunk.title,
                section=chunk.section,
                content=chunk.text,
            )
            for chunk in chunks
        ],
    )
    return embedding, vector_index, keyword_index, chunks


async def _build_offline(
    settings: Settings, corpus_dir: Path, version: str
) -> tuple[HybridRetriever, str]:
    """离线装配：无 MySQL/Chroma，但 embedding 走配置（fastembed 时是真实语义分）。"""
    embedding, vector_index, keyword_index, chunks = _offline_components(
        settings, corpus_dir, version
    )
    collection_id = await vector_index.create_collection(collection_name(version))
    vectors = await embedding.embed([chunk.text for chunk in chunks])
    await vector_index.upsert(
        collection_id,
        ids=[chunk_id_of(chunk.doc_id, chunk.index) for chunk in chunks],
        embeddings=vectors,
        documents=[chunk.text for chunk in chunks],
        metadatas=[chunk.to_metadata() for chunk in chunks],
    )
    retriever = HybridRetriever(
        embedding=embedding,
        vector_index=vector_index,
        keyword_index=keyword_index,
        settings=settings,
    )
    return retriever, collection_id


async def _online_retriever(
    settings: Settings, clients: AppClients, version: str | None
) -> tuple[HybridRetriever, str, str]:
    indexer = Indexer(
        settings=settings,
        embedding=build_embedding(settings),
        vector_index=ChromaVectorIndex(settings.chroma_url),
        session_factory=clients.session_factory,
    )
    active = version or await indexer.active_version()
    if not active:
        raise RuntimeError("没有 active 索引版本，请先执行 rebuild --version v1 或显式传 --version")
    async with clients.session_factory() as session:
        from sqlalchemy import select

        from app.db.models import IndexVersion

        row = await session.scalar(select(IndexVersion).where(IndexVersion.version == active))
        if row is None:
            raise RuntimeError(f"索引版本不存在：{active}")
    chroma = ChromaVectorIndex(settings.chroma_url)
    collection_id = await chroma.create_collection(row.collection)
    retriever = HybridRetriever(
        embedding=build_embedding(settings),
        vector_index=chroma,
        keyword_index=MysqlKeywordIndex(clients.engine),
        settings=settings,
    )
    return retriever, active, collection_id


async def _run_rebuild(
    settings: Settings, clients: AppClients, version: str, corpus_dir: Path, activate: bool
) -> str:
    indexer = Indexer(
        settings=settings,
        embedding=build_embedding(settings),
        vector_index=ChromaVectorIndex(settings.chroma_url),
        session_factory=clients.session_factory,
        corpus_dir=corpus_dir,
    )
    result = await indexer.build(version, activate=activate)
    return json.dumps(result.as_dict(), ensure_ascii=False, indent=2)


async def _run_versions(clients: AppClients, settings: Settings) -> str:
    indexer = Indexer(
        settings=settings,
        embedding=build_embedding(settings),
        vector_index=ChromaVectorIndex(settings.chroma_url),
        session_factory=clients.session_factory,
    )
    return json.dumps(await indexer.versions(), ensure_ascii=False, indent=2)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m app.rag.cli", description="RAG 索引与检索 CLI")
    sub = parser.add_subparsers(dest="command", required=True)

    query = sub.add_parser("query", help="检索（离线/在线）")
    query.add_argument("text", help="查询语句")
    query.add_argument("--version", default=None, help="索引版本（在线模式默认取 active）")
    query.add_argument("--offline", action="store_true", help="离线模式：hash embedding + 内存索引")
    query.add_argument("--top-k", type=int, default=None, help="返回条数，默认取 RAG_TOP_K_FINAL")
    query.add_argument("--json", action="store_true", help="输出 JSON")

    rebuild = sub.add_parser("rebuild", help="构建/重建索引（需要 MySQL + Chroma）")
    rebuild.add_argument("--version", required=True)
    rebuild.add_argument("--corpus", default=None, help="语料目录，默认 CORPUS_DIR")
    rebuild.add_argument("--no-activate", action="store_true", help="只构建不激活（用于灰度校验）")

    sub.add_parser("versions", help="列出索引版本")
    return parser


async def _amain(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    settings = get_settings()
    configure_logging(settings.log_level, "console")
    corpus_dir = Path(args.corpus) if getattr(args, "corpus", None) else Path(settings.corpus_dir)

    if args.command == "query" and args.offline:
        version = args.version or "v1"
        retriever, collection_id = await _build_offline(settings, corpus_dir, version)
        result = await retriever.retrieve(
            args.text, index_version=version, collection_id=collection_id, top_k=args.top_k
        )
        print(render_result(result, as_json=args.json))
        return EXIT_OK

    clients = build_clients(settings)
    try:
        if args.command == "query":
            retriever, version, collection_id = await _online_retriever(
                settings, clients, args.version
            )
            result = await retriever.retrieve(
                args.text, index_version=version, collection_id=collection_id, top_k=args.top_k
            )
            print(render_result(result, as_json=args.json))
            return EXIT_OK
        if args.command == "rebuild":
            print(
                await _run_rebuild(
                    settings, clients, args.version, corpus_dir, not args.no_activate
                )
            )
            return EXIT_OK
        if args.command == "versions":
            print(await _run_versions(clients, settings))
            return EXIT_OK
        parser.error(f"未知命令：{args.command}")
    except Exception as exc:
        print(f"[error] {type(exc).__name__}: {exc}", file=sys.stderr)
        return EXIT_ERROR
    finally:
        await clients.aclose()


def main(argv: list[str] | None = None) -> int:
    return asyncio.run(_amain(argv))


if __name__ == "__main__":
    sys.exit(main())
