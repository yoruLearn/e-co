"""引用标记（Spec §7.2 第 5 条 / G2）。

约定：模型在事实句后用 ``[[n]]`` 标注引用编号，n 对应传入 <资料> 的序号。
解析与上下文构造都在这里，保证"生成层"和"校验层"用同一套规则。
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from typing import Final

from app.agent.state import Citation
from app.rag.pipeline import RetrievalHit

MARKER_RE: Final[re.Pattern[str]] = re.compile(r"\[\[(\d+)\]\]")


def referenced_ids(text: str) -> list[int]:
    """按出现顺序返回去重后的引用编号。"""
    seen: list[int] = []
    for match in MARKER_RE.finditer(text or ""):
        value = int(match.group(1))
        if value not in seen:
            seen.append(value)
    return seen


def has_citation(text: str) -> bool:
    return bool(MARKER_RE.search(text or ""))


def build_context_blocks(
    hits: Sequence[RetrievalHit], *, max_chars_per_hit: int = 500
) -> list[str]:
    """构造 <资料> 文本块：``[1] 标题 / 节`` + 正文（编号必须与 Citation.id 一致）。"""
    blocks: list[str] = []
    for index, hit in enumerate(hits, start=1):
        section = hit.section or hit.title
        body = hit.content[:max_chars_per_hit].strip()
        blocks.append(f"[{index}] {hit.title} / {section}\n{body}")
    return blocks


def to_citations(hits: Sequence[RetrievalHit]) -> list[Citation]:
    """检索命中 → 引用模型（id 从 1 开始，与 ``[[n]]`` 对齐）。"""
    return [
        Citation(
            id=index,
            doc_id=hit.doc_id,
            title=hit.title,
            section=hit.section,
            snippet=hit.content[:200],
            score=round(hit.score, 4),
        )
        for index, hit in enumerate(hits, start=1)
    ]


def citation_for_marker(citations: Sequence[Citation], marker_id: int) -> Citation | None:
    for citation in citations:
        if citation.id == marker_id:
            return citation
    return None
