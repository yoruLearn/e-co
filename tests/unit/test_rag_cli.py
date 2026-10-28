"""RAG CLI 离线装配与结果渲染测试（不依赖 MySQL/Chroma）。"""

from __future__ import annotations

import json
from pathlib import Path

from app.common.config import REPO_ROOT, Settings
from app.rag.cache import answer_cache_key
from app.rag.cli import _build_offline, render_result
from app.rag.pipeline import RetrievalHit, RetrievalResult
from app.rag.retriever import ConfidenceBand

CORPUS_DIR = REPO_ROOT / "seeds" / "corpus"


def make_settings() -> Settings:
    return Settings(
        _env_file=None,
        app_env="test",
        log_level="ERROR",
        log_format="console",
        embedding_provider="hash",
        embedding_dim=64,
    )


def sample_result() -> RetrievalResult:
    return RetrievalResult(
        query="退款多久到账",
        index_version="v1",
        hits=[
            RetrievalHit(
                chunk_id="policy#0",
                doc_id="policy",
                title="售后政策总则",
                section="退款时效",
                content="余额 1 个工作日到账。",
                score=0.91,
            )
        ],
        band=ConfidenceBand.ANSWER,
        degraded=["vector:ok"],
    )


def test_render_result_text() -> None:
    text = render_result(sample_result())
    assert "[1]" in text
    assert "退款时效" in text
    assert "answer" in text


def test_render_result_json_is_parseable() -> None:
    payload = json.loads(render_result(sample_result(), as_json=True))
    assert payload["query"] == "退款多久到账"
    assert payload["hits"][0]["chunk_id"] == "policy#0"
    assert payload["band"] == "answer"


async def test_build_offline_retrieves_from_corpus() -> None:
    retriever, collection_id = await _build_offline(make_settings(), CORPUS_DIR, "v-test")
    result = await retriever.retrieve(
        "退款多久到账", index_version="v-test", collection_id=collection_id
    )
    assert result.hits, "离线索引应能召回语料"
    assert result.band in {ConfidenceBand.ANSWER, ConfidenceBand.ANSWER_WITH_CAVEAT}


def test_answer_cache_key_bound_to_version_and_normalized() -> None:
    first = answer_cache_key("v1", "  退款多久到账 ")
    second = answer_cache_key("v1", "退款多久到账")
    other = answer_cache_key("v2", "退款多久到账")
    assert first == second
    assert first != other
    assert first.startswith("ans:v1:")


def test_corpus_dir_exists() -> None:
    assert Path(CORPUS_DIR).is_dir()
