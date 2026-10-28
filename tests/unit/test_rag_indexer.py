"""Indexer 纯逻辑测试：构建计划、分块装载、哈希与批量切分（不触碰 DB/Chroma）。"""

from __future__ import annotations

import pytest

from app.common.config import REPO_ROOT, Settings
from app.llm.embeddings import HashEmbedding
from app.rag.chroma import InMemoryVectorIndex
from app.rag.errors import IndexBuildError
from app.rag.indexer import (
    CorpusStats,
    IndexBuildPlan,
    IndexBuildResult,
    Indexer,
    _batched,
    content_hash,
)

CORPUS_DIR = REPO_ROOT / "seeds" / "corpus"


def make_settings() -> Settings:
    return Settings(_env_file=None, app_env="test", log_level="ERROR", log_format="console")


def build_indexer(corpus_dir=CORPUS_DIR) -> Indexer:
    return Indexer(
        settings=make_settings(),
        embedding=HashEmbedding(dim=8),
        vector_index=InMemoryVectorIndex(),
        session_factory=None,  # type: ignore[arg-type]
        corpus_dir=corpus_dir,
    )


def test_build_plan_for_version() -> None:
    plan = IndexBuildPlan.for_version("v9", CORPUS_DIR, "hash", chunk_size=400, chunk_overlap=50)
    assert plan.collection == "aftersales_v9"
    assert plan.chunk_size == 400
    assert plan.chunk_overlap == 50


def test_content_hash_is_stable_and_sensitive() -> None:
    assert content_hash("abc") == content_hash("abc")
    assert content_hash("abc") != content_hash("abd")
    assert len(content_hash("abc")) == 64


def test_batched_splits_evenly() -> None:
    class Item:
        def __init__(self, value: int) -> None:
            self.value = value

    items = [Item(index) for index in range(5)]  # type: ignore[list-item]
    batches = _batched(items, 2)  # type: ignore[arg-type]
    assert [len(batch) for batch in batches] == [2, 2, 1]
    assert _batched([], 2) == []


def test_load_chunks_from_corpus() -> None:
    chunks, stats = build_indexer()._load_chunks("v-test")
    assert chunks
    assert stats.documents >= 5
    assert stats.chunks == len(chunks)
    assert stats.chars == sum(chunk.char_count for chunk in chunks)


def test_load_chunks_empty_dir_raises(tmp_path) -> None:
    with pytest.raises(IndexBuildError):
        build_indexer(tmp_path)._load_chunks("v-test")


def test_stats_and_result_as_dict() -> None:
    stats = CorpusStats(documents=1, chunks=2, chars=30)
    assert stats.as_dict() == {"documents": 1, "chunks": 2, "chars": 30}
    result = IndexBuildResult(
        version="v1",
        collection="aftersales_v1",
        collection_id="c1",
        stats=stats,
        batches=1,
        activated=True,
        duration_ms=12,
    )
    payload = result.as_dict()
    assert payload["version"] == "v1"
    assert payload["stats"] == {"documents": 1, "chunks": 2, "chars": 30}
