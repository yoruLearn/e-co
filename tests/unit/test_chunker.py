"""分块测试（Spec §7.1）：标题层级、500/80 滑窗、元数据、示例语料规模。

这些用例同时充当"语料没有写坏"的守卫：示例语料必须能被切成可检索的块。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from app.rag.chunker import (
    CHUNK_OVERLAP,
    CHUNK_SIZE,
    Chunk,
    chunk_document,
    chunk_text,
    collection_name,
    extract_title,
    load_corpus,
    load_markdown_file,
    normalize_markdown,
    split_sections,
    split_sentences,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
CORPUS_DIR = REPO_ROOT / "seeds" / "corpus"


def test_collection_name_normalizes_version() -> None:
    assert collection_name("v3") == "aftersales_v3"
    assert collection_name("V7") == "aftersales_v7"
    assert collection_name("12") == "aftersales_v12"
    with pytest.raises(ValueError, match="不能为空"):
        collection_name("  ")


def test_normalize_markdown_collapses_noise() -> None:
    raw = "标题\r\n\r\n\r\n\r\n正文   有多余   空格  \r\n"
    assert normalize_markdown(raw) == "标题\n\n正文 有多余 空格"


def test_split_sentences_keeps_delimiters() -> None:
    sentences = split_sentences("第一句。第二句！第三句？")
    assert sentences == ["第一句。", "第二句！", "第三句？"]
    assert split_sentences("") == []
    assert split_sentences("没有标点") == ["没有标点"]


def test_extract_title_reads_h1() -> None:
    assert extract_title("# 售后政策\n正文") == "售后政策"
    assert extract_title("无标题", fallback="兜底") == "兜底"


def test_split_sections_builds_hierarchy_path() -> None:
    markdown = "# 文档\n## 退款\n退款说明。\n### 到账时间\n到账说明。\n## 发票\n发票说明。\n"
    sections = split_sections(markdown)
    assert [section.path for section in sections] == ["退款", "退款 > 到账时间", "发票"]
    assert sections[0].text == "退款说明。"


def test_split_sections_without_headings_uses_default_title() -> None:
    sections = split_sections("只有正文。", default_title="总则")
    assert sections[0].path == "总则"
    assert sections[0].text == "只有正文。"


def test_chunk_text_short_and_empty() -> None:
    assert chunk_text("") == []
    assert chunk_text("   ") == []
    assert chunk_text("短文本。") == ["短文本。"]


def test_chunk_text_respects_size_limit() -> None:
    text = "这是一句用于测试分块的中文句子。" * 120  # 约 2040 字
    chunks = chunk_text(text)
    assert len(chunks) > 1
    assert all(len(chunk) <= CHUNK_SIZE for chunk in chunks)


def test_chunk_text_has_overlap_between_neighbours() -> None:
    text = "".join(f"第{i}条，售后条款内容示例，用于验证重叠。" for i in range(40))
    chunks = chunk_text(text)
    assert len(chunks) >= 2
    first_tail = chunks[0][-CHUNK_OVERLAP:]
    assert any(part in chunks[1] for part in (first_tail, first_tail[-20:]))


def test_chunk_text_hard_splits_oversized_sentence() -> None:
    long_sentence = "无标点长句" * 400  # 2000 字且无句末标点
    chunks = chunk_text(long_sentence)
    assert len(chunks) > 1
    assert all(len(chunk) <= CHUNK_SIZE for chunk in chunks)


def test_chunk_text_rejects_bad_params() -> None:
    with pytest.raises(ValueError, match="CHUNK_SIZE"):
        chunk_text("x", size=0)
    with pytest.raises(ValueError, match="CHUNK_OVERLAP"):
        chunk_text("x", size=100, overlap=100)


def test_chunk_document_metadata_is_complete() -> None:
    markdown = (
        "# 售后政策\n## 七天无理由\n自签收次日起七日内可申请。\n## 退款时效\n余额 1 个工作日到账。"
    )
    chunks = chunk_document(markdown, doc_id="policy-1", title="", version="v1")
    assert len(chunks) == 2
    first = chunks[0]
    assert isinstance(first, Chunk)
    assert first.doc_id == "policy-1"
    assert first.title == "售后政策"
    assert first.section == "七天无理由"
    assert first.version == "v1"
    assert first.index == 0
    assert chunks[1].index == 1
    metadata = first.to_metadata()
    assert metadata["doc_id"] == "policy-1"
    assert metadata["section"] == "七天无理由"
    assert metadata["version"] == "v1"


def test_chunk_document_handles_empty_input() -> None:
    assert chunk_document("", doc_id="d", title="t", version="v1") == []


# --------------------------------------------------------------------------- #
# 示例语料守卫
# --------------------------------------------------------------------------- #
def test_corpus_loads_and_is_chunked() -> None:
    documents = load_corpus(CORPUS_DIR, version="v1")
    assert len(documents) >= 5, "示例语料应包含 3 篇政策 + FAQ + 商品手册"
    all_chunks = [chunk for document in documents for chunk in document.chunks]
    assert len(all_chunks) >= 60, f"示例语料块数过少：{len(all_chunks)}"
    assert all(chunk.text.strip() for chunk in all_chunks)
    assert all(len(chunk.text) <= CHUNK_SIZE for chunk in all_chunks)
    assert {chunk.doc_id for chunk in all_chunks} == {document.doc_id for document in documents}
    # 每块都能回溯到文档与节
    assert all(chunk.doc_id and chunk.title for chunk in all_chunks)


def test_corpus_faq_has_forty_qa_sections() -> None:
    document = load_markdown_file(CORPUS_DIR / "售后常见问题.md", version="v1")
    qa_sections = [chunk for chunk in document.chunks if re.search(r"Q\d{2}", chunk.section)]
    assert len(qa_sections) == 40, f"FAQ 应为 40 条，实际 {len(qa_sections)}"


def test_corpus_documents_have_unique_ids() -> None:
    documents = load_corpus(CORPUS_DIR, version="v1")
    ids = [document.doc_id for document in documents]
    assert len(ids) == len(set(ids))


def test_corpus_contains_key_policy_facts() -> None:
    """语料必须覆盖评测会用到的关键事实，防止"改文档把评测改坏"。"""
    text = "\n".join(
        chunk.text
        for document in load_corpus(CORPUS_DIR, version="v1")
        for chunk in document.chunks
    )
    for keyword in ["七天无理由", "十五天", "退款", "运费", "发票", "价保", "保修"]:
        assert keyword in text, f"示例语料缺少关键事实：{keyword}"
