"""RAG 端到端冒烟：真实 MySQL + Chroma（CI job / 本地起栈后均可运行）。

验证的不是"检索质量"（那是评测集的事），而是**管道连通性**：

1. `alembic upgrade head` 之后的 9+1 张表可用；
2. 语料 → 分块 → 向量化 → Chroma + `rag_chunk` 的条数完全一致；
3. `index_version` 从 building 变为 active；
4. 在线混合召回返回命中且无降级；
5. 关键词单通道（collection_id=None）也能返回命中 —— 证明 ngram 索引真的建成了。

用法::

    uv run python scripts/rag_smoke.py --version v1
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from sqlalchemy import func, select

from app.common.clients import build_clients
from app.common.config import get_settings
from app.common.logging import configure_logging
from app.db.models import RagChunk
from app.llm.factory import build_embedding
from app.prompt.registry import PromptRegistry
from app.prompt.seed import seed_from_yaml
from app.rag.chroma import ChromaVectorIndex, wait_for_chroma
from app.rag.chunker import collection_name, load_corpus
from app.rag.indexer import Indexer
from app.rag.keyword import MysqlKeywordIndex
from app.rag.pipeline import HybridRetriever


async def main(version: str, corpus_dir: Path) -> int:
    settings = get_settings()
    configure_logging(settings.log_level, "console")
    clients = build_clients(settings)
    chroma = ChromaVectorIndex(settings.chroma_url)
    try:
        if not await wait_for_chroma(chroma, timeout_s=90):
            print(f"[rag-smoke] Chroma 未就绪：{settings.chroma_url}", file=sys.stderr)
            return 1

        documents = load_corpus(corpus_dir, version=version)
        expected_chunks = sum(len(document.chunks) for document in documents)
        print(f"[rag-smoke] corpus docs={len(documents)} chunks={expected_chunks}")

        indexer = Indexer(
            settings=settings,
            embedding=build_embedding(settings),
            vector_index=chroma,
            session_factory=clients.session_factory,
            corpus_dir=corpus_dir,
        )
        build = await indexer.build(version)
        print(f"[rag-smoke] build {json.dumps(build.as_dict(), ensure_ascii=False)}")
        if build.stats.chunks != expected_chunks:
            print("[rag-smoke] 分块数量与语料不一致", file=sys.stderr)
            return 1

        async with clients.session_factory() as session:
            stored = await session.scalar(
                select(func.count()).select_from(RagChunk).where(RagChunk.index_version == version)
            )
        print(f"[rag-smoke] rag_chunk rows={stored}")
        if stored != expected_chunks:
            print("[rag-smoke] rag_chunk 行数与分块不一致", file=sys.stderr)
            return 1

        active = await indexer.active_version()
        print(f"[rag-smoke] active_version={active}")
        if active != version:
            print("[rag-smoke] 索引未激活", file=sys.stderr)
            return 1

        collection_id = await chroma.create_collection(collection_name(version))
        retriever = HybridRetriever(
            embedding=build_embedding(settings),
            vector_index=chroma,
            keyword_index=MysqlKeywordIndex(clients.engine),
            settings=settings,
        )
        hybrid = await retriever.retrieve(
            "退款多久到账", index_version=version, collection_id=collection_id
        )
        print(
            f"[rag-smoke] hybrid hits={len(hybrid.hits)} band={hybrid.band} "
            f"top1={hybrid.top1_score:.4f} degraded={hybrid.degraded}"
        )
        if not hybrid.hits or hybrid.degraded:
            print("[rag-smoke] 混合召回失败或有降级", file=sys.stderr)
            return 1

        keyword_only = await retriever.retrieve(
            "退货运费", index_version=version, collection_id=None
        )
        print(f"[rag-smoke] keyword-only hits={len(keyword_only.hits)} band={keyword_only.band}")
        if not keyword_only.hits:
            print("[rag-smoke] 关键词通道无命中（ngram 索引可能未生效）", file=sys.stderr)
            return 1

        # Prompt Registry：先灌基线，再从 DB 读回（验证 DB 源优先于 seed 源）
        seeded = await seed_from_yaml(
            session_factory=clients.session_factory,
            path=Path(settings.prompt_seed_path),
        )
        registry = PromptRegistry.from_settings(settings, session_factory=clients.session_factory)
        template = await registry.get("answer_rag")
        print(f"[rag-smoke] prompts seeded={seeded} source={template.source} v={template.version}")
        if template.source != "db" or "[[" not in template.content:
            print("[rag-smoke] Prompt Registry 未从 DB 读到基线", file=sys.stderr)
            return 1

        print("[rag-smoke] OK")
        return 0
    finally:
        await chroma.aclose()
        await clients.aclose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="RAG 端到端冒烟")
    parser.add_argument("--version", default="v1")
    parser.add_argument("--corpus", default="seeds/corpus")
    args = parser.parse_args()
    sys.exit(asyncio.run(main(args.version, Path(args.corpus))))
