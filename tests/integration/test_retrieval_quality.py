"""检索质量抽样（本地真实 embedding，默认跳过）。

CI 用 hash 向量只能验证管道；本文件用 fastembed 的 bge-small-zh 对示例语料做
"问句 → Top1 是否落在正确政策条目"的冒烟。它不是完整评测（M1-10 才有 90 条），
但如果这里都不过，说明语料/分块/融合有结构性问题，先修再谈评测。

运行：``RUN_LOCAL_EMBED=1 pytest tests/integration/test_retrieval_quality.py -q``
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from app.common.config import Settings
from app.rag.cli import _build_offline

pytestmark = pytest.mark.local_embed

REPO_ROOT = Path(__file__).resolve().parents[2]
CORPUS_DIR = REPO_ROOT / "seeds" / "corpus"


@pytest.fixture(scope="module")
def settings() -> Settings:
    if os.environ.get("RUN_LOCAL_EMBED") != "1":
        pytest.skip("需要本地 fastembed 模型；RUN_LOCAL_EMBED=1 时运行")
    pytest.importorskip("fastembed")
    return Settings(
        _env_file=None,
        app_env="test",
        embedding_provider="fastembed",
        embedding_model="BAAI/bge-small-zh-v1.5",
        embedding_dim=512,
        hf_endpoint="https://hf-mirror.com",
        rag_top1_threshold=0.75,
        rag_low_threshold=0.55,
    )


@pytest.mark.parametrize(
    ("query", "expected_keywords"),
    [
        ("退款多久能到账", ["退款", "工作日"]),
        ("七天无理由从哪天开始算", ["七", "签收"]),
        ("退货运费谁承担", ["运费"]),
        ("发票可以重开吗", ["发票"]),
        ("耳机保修多久", ["保修"]),
    ],
)
async def test_top_hits_cover_expected_policy(
    settings: Settings, query: str, expected_keywords: list[str]
) -> None:
    retriever, collection_id = await _build_offline(settings, CORPUS_DIR, "v1")
    result = await retriever.retrieve(query, index_version="v1", collection_id=collection_id)
    assert result.hits, f"没有召回：{query}"
    joined = " ".join(f"{hit.title} {hit.section} {hit.content}" for hit in result.hits[:2])
    missing = [keyword for keyword in expected_keywords if keyword not in joined]
    assert not missing, f"{query} 的 Top2 未覆盖关键词 {missing}；实际 Top2={joined[:200]}"
    assert result.band != "fallback", f"{query} 被判低置信，top1={result.top1_score:.3f}"


async def test_degradation_returns_keyword_only(settings: Settings) -> None:
    retriever, _collection_id = await _build_offline(settings, CORPUS_DIR, "v1")
    result = await retriever.retrieve("退货款什么时候到", index_version="v1", collection_id=None)
    assert result.vector_ok is False
    assert "vector:no_collection" in result.degraded
    assert result.hits
