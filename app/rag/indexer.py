"""离线索引构建（Spec §7.1）：文档 → 分块 → 向量化 → Chroma + `rag_chunk` → 版本流转。

一次构建（幂等，可重复执行）：

1. 建/取 Chroma collection（``aftersales_v{N}``，cosine）；
2. 分批（32/批）embedding 并 upsert 向量 + 原文 + 元数据；
3. 覆盖写 ``rag_chunk``（同版本先删后插，保证重建幂等）；
4. ``index_version``：旧 active → deprecated，当前版本 building → active。

回滚：``activate(version)`` 只改状态，不动数据（旧 collection 与旧分块保留）。
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path

from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.common.config import Settings
from app.common.logging import get_logger, log_event
from app.db.models import IndexVersion, RagChunk
from app.llm.base import EmbeddingModel
from app.rag.chroma import ChromaVectorIndex, VectorIndex
from app.rag.chunker import (
    CHUNK_OVERLAP,
    CHUNK_SIZE,
    EMBEDDING_BATCH_SIZE,
    Chunk,
    collection_name,
    load_corpus,
)
from app.rag.errors import IndexBuildError

logger = get_logger("app.rag.indexer")


class IndexStatus(StrEnum):
    BUILDING = "building"
    ACTIVE = "active"
    DEPRECATED = "deprecated"
    ROLLED_BACK = "rolled_back"


@dataclass(frozen=True, slots=True)
class IndexBuildPlan:
    """一次索引构建的输入（CLI/批量脚本使用）。"""

    version: str
    source_dir: Path
    collection: str
    embedding_model: str
    chunk_size: int = CHUNK_SIZE
    chunk_overlap: int = CHUNK_OVERLAP

    @classmethod
    def for_version(
        cls,
        version: str,
        source_dir: Path,
        embedding_model: str,
        *,
        chunk_size: int = CHUNK_SIZE,
        chunk_overlap: int = CHUNK_OVERLAP,
    ) -> IndexBuildPlan:
        return cls(
            version=version,
            source_dir=source_dir,
            collection=collection_name(version),
            embedding_model=embedding_model,
            chunk_size=chunk_size,
            chunk_overlap=chunk_overlap,
        )


@dataclass(frozen=True, slots=True)
class CorpusStats:
    documents: int
    chunks: int
    chars: int

    def as_dict(self) -> dict[str, int]:
        return {"documents": self.documents, "chunks": self.chunks, "chars": self.chars}


@dataclass(frozen=True, slots=True)
class IndexBuildResult:
    version: str
    collection: str
    collection_id: str
    stats: CorpusStats
    batches: int
    activated: bool
    duration_ms: int

    def as_dict(self) -> dict[str, object]:
        return {
            "version": self.version,
            "collection": self.collection,
            "collection_id": self.collection_id,
            "stats": self.stats.as_dict(),
            "batches": self.batches,
            "activated": self.activated,
            "duration_ms": self.duration_ms,
        }


def content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _batched(items: list[Chunk], size: int) -> list[list[Chunk]]:
    return [items[index : index + size] for index in range(0, len(items), size)]


class Indexer:
    def __init__(
        self,
        *,
        settings: Settings,
        embedding: EmbeddingModel,
        vector_index: VectorIndex,
        session_factory: async_sessionmaker[AsyncSession],
        corpus_dir: Path | None = None,
    ) -> None:
        self._settings = settings
        self._embedding = embedding
        self._vector = vector_index
        self._sessions = session_factory
        self._corpus_dir = corpus_dir or Path(settings.corpus_dir)

    def _load_chunks(self, version: str) -> tuple[list[Chunk], CorpusStats]:
        documents = load_corpus(self._corpus_dir, version=version)
        if not documents:
            raise IndexBuildError(
                f"语料目录为空：{self._corpus_dir}",
                detail={"corpus_dir": str(self._corpus_dir)},
            )
        chunks = [chunk for document in documents for chunk in document.chunks]
        stats = CorpusStats(
            documents=len(documents),
            chunks=len(chunks),
            chars=sum(chunk.char_count for chunk in chunks),
        )
        return chunks, stats

    async def build(
        self,
        version: str,
        *,
        activate: bool = True,
        rebuild: bool = True,
    ) -> IndexBuildResult:
        started = datetime.now(tz=UTC)
        if not version.strip():
            raise IndexBuildError("索引版本不能为空")
        collection = collection_name(version)
        chunks, stats = self._load_chunks(version)
        log_event(
            logger,
            "index.build.start",
            version=version,
            documents=stats.documents,
            chunks=stats.chunks,
        )

        collection_id = await self._vector.create_collection(collection)
        await self._mark_building(version, collection, stats.chunks)

        batches = _batched(chunks, EMBEDDING_BATCH_SIZE)
        for batch_index, batch in enumerate(batches, start=1):
            vectors = await self._embedding.embed([chunk.text for chunk in batch])
            if len(vectors) != len(batch):
                raise IndexBuildError(
                    "embedding 返回数量与分块数量不一致",
                    detail={"expected": len(batch), "actual": len(vectors)},
                )
            await self._vector.upsert(
                collection_id,
                ids=[f"{chunk.doc_id}#{chunk.index}" for chunk in batch],
                embeddings=vectors,
                documents=[chunk.text for chunk in batch],
                metadatas=[chunk.to_metadata() for chunk in batch],
            )
            log_event(
                logger,
                "index.build.batch",
                version=version,
                batch=batch_index,
                total_batches=len(batches),
            )

        async with self._sessions() as session:
            if rebuild:
                await session.execute(delete(RagChunk).where(RagChunk.index_version == version))
            session.add_all(
                [
                    RagChunk(
                        index_version=version,
                        doc_id=chunk.doc_id,
                        title=chunk.title,
                        section=chunk.section,
                        chunk_index=chunk.index,
                        content=chunk.text,
                        content_hash=content_hash(chunk.text),
                        char_count=chunk.char_count,
                    )
                    for chunk in chunks
                ]
            )
            await session.commit()

        if activate:
            await self.activate(version, collection=collection, doc_count=stats.chunks)

        duration_ms = int((datetime.now(tz=UTC) - started).total_seconds() * 1000)
        log_event(
            logger,
            "index.build.done",
            version=version,
            chunks=stats.chunks,
            batches=len(batches),
            duration_ms=duration_ms,
            activated=activate,
        )
        return IndexBuildResult(
            version=version,
            collection=collection,
            collection_id=collection_id,
            stats=stats,
            batches=len(batches),
            activated=activate,
            duration_ms=duration_ms,
        )

    async def _mark_building(self, version: str, collection: str, doc_count: int) -> None:
        async with self._sessions() as session:
            existing = await session.scalar(
                select(IndexVersion).where(IndexVersion.version == version)
            )
            if existing is None:
                session.add(
                    IndexVersion(
                        version=version,
                        collection=collection,
                        doc_count=doc_count,
                        status=IndexStatus.BUILDING,
                    )
                )
            else:
                existing.status = IndexStatus.BUILDING
                existing.doc_count = doc_count
                existing.collection = collection
            await session.commit()

    async def activate(
        self,
        version: str,
        *,
        collection: str | None = None,
        doc_count: int | None = None,
    ) -> str:
        """激活版本：旧 active 置 deprecated，目标版本置 active。"""
        async with self._sessions() as session:
            await session.execute(
                update(IndexVersion)
                .where(IndexVersion.status == IndexStatus.ACTIVE, IndexVersion.version != version)
                .values(status=IndexStatus.DEPRECATED)
            )
            existing = await session.scalar(
                select(IndexVersion).where(IndexVersion.version == version)
            )
            if existing is None:
                if collection is None:
                    raise IndexBuildError(
                        f"索引版本不存在且未提供 collection：{version}",
                        detail={"version": version},
                    )
                session.add(
                    IndexVersion(
                        version=version,
                        collection=collection,
                        doc_count=doc_count or 0,
                        status=IndexStatus.ACTIVE,
                    )
                )
            else:
                existing.status = IndexStatus.ACTIVE
                if collection:
                    existing.collection = collection
                if doc_count is not None:
                    existing.doc_count = doc_count
            await session.commit()
        log_event(logger, "index.activate", version=version)
        return version

    async def active_version(self) -> str | None:
        async with self._sessions() as session:
            row = await session.scalar(
                select(IndexVersion).where(IndexVersion.status == IndexStatus.ACTIVE)
            )
        return row.version if row is not None else None

    async def versions(self) -> list[dict[str, object]]:
        async with self._sessions() as session:
            rows = (
                await session.scalars(select(IndexVersion).order_by(IndexVersion.id.desc()))
            ).all()
        return [
            {
                "version": row.version,
                "collection": row.collection,
                "doc_count": row.doc_count,
                "status": row.status,
                "eval_score": float(row.eval_score) if row.eval_score is not None else None,
                "created_at": row.created_at.isoformat() if row.created_at else None,
            }
            for row in rows
        ]

    async def aclose(self) -> None:
        if isinstance(self._vector, ChromaVectorIndex):
            await self._vector.aclose()
