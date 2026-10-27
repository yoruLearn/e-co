"""文档分块（Spec §7.1）：按标题层级切分 + 500 字 / 80 overlap 滑窗。

切分策略（为中文售后文档调过参）：

1. 先按 Markdown 标题（``##`` / ``###``…）切成"节"，节路径记录为 ``售后政策总则 > 退款时效``；
2. 节内按句子（``。！？；`` 与换行）聚合，尽量不把一句话切断；
3. 单节超长时按 500 字切，下一块带上**上一块末尾 80 字**做 overlap，且从句子边界开始；
4. 单句本身超 500 字（长表格/长条款）时硬切，仍保留 overlap。

产出 ``Chunk`` 携带 ``doc_id / title / section / version``，直接可写 Chroma metadata 与
``rag_chunk`` 表（M1 迁移 0002）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

CHUNK_SIZE: Final[int] = 500
CHUNK_OVERLAP: Final[int] = 80
EMBEDDING_BATCH_SIZE: Final[int] = 32
COLLECTION_PREFIX: Final[str] = "aftersales_v"

_HEADING_RE: Final[re.Pattern[str]] = re.compile(r"^(#{2,6})\s+(.+?)\s*$")
_TITLE_RE: Final[re.Pattern[str]] = re.compile(r"^#\s+(.+?)\s*$")
_SENTENCE_END: Final[str] = "。！？；!?;\n"
_MULTI_NEWLINE: Final[re.Pattern[str]] = re.compile(r"\n{3,}")
_SPACES: Final[re.Pattern[str]] = re.compile(r"[ \t]{2,}")


@dataclass(frozen=True, slots=True)
class Chunk:
    """可入库的最小检索单元。"""

    doc_id: str
    title: str
    section: str
    version: str
    text: str
    index: int = 0
    metadata: dict[str, str] = field(default_factory=dict)

    def to_metadata(self) -> dict[str, str]:
        merged = {
            "doc_id": self.doc_id,
            "title": self.title,
            "section": self.section,
            "version": self.version,
            "index": str(self.index),
        }
        merged.update(self.metadata)
        return merged

    @property
    def char_count(self) -> int:
        return len(self.text)


def collection_name(index_version: str) -> str:
    """``v3`` → ``aftersales_v3``。"""
    cleaned = index_version.strip()
    if not cleaned:
        msg = "索引版本不能为空"
        raise ValueError(msg)
    return f"{COLLECTION_PREFIX}{cleaned.lstrip('vV')}"


def normalize_markdown(text: str) -> str:
    """折叠多余空行/行内空格，保留段落结构（影响句子切分与向量质量）。"""
    collapsed = _MULTI_NEWLINE.sub("\n\n", text.replace("\r\n", "\n").replace("\r", "\n"))
    collapsed = _SPACES.sub(" ", collapsed)
    return collapsed.strip()


def split_sentences(text: str) -> list[str]:
    """按中英文句末标点与换行切句，保留标点。"""
    sentences: list[str] = []
    buffer = ""
    for char in text:
        buffer += char
        if char in _SENTENCE_END:
            sentences.append(buffer)
            buffer = ""
    if buffer.strip():
        sentences.append(buffer)
    return [sentence for sentence in sentences if sentence.strip()]


def _overlap_tail(chunk: str, overlap: int) -> str:
    """取上一块尾部 ``overlap`` 字，尽量从句子边界开始，避免半句开场。"""
    if overlap <= 0 or not chunk:
        return ""
    tail = chunk[-overlap:]
    for index, char in enumerate(tail):
        if char in _SENTENCE_END:
            candidate = tail[index + 1 :]
            if candidate.strip():
                return candidate
    return tail


def _hard_split(text: str, size: int, overlap: int) -> list[str]:
    """单句超长时硬切，保留 overlap。"""
    parts: list[str] = []
    start = 0
    step = max(size - overlap, 1)
    while start < len(text):
        parts.append(text[start : start + size])
        if start + size >= len(text):
            break
        start += step
    return [part for part in parts if part.strip()]


def chunk_text(text: str, *, size: int = CHUNK_SIZE, overlap: int = CHUNK_OVERLAP) -> list[str]:
    """把一段正文切成 ``size`` 字、相邻 ``overlap`` 重叠的块。"""
    if size <= 0:
        msg = "CHUNK_SIZE 必须为正"
        raise ValueError(msg)
    if not 0 <= overlap < size:
        msg = "CHUNK_OVERLAP 必须满足 0 <= overlap < size"
        raise ValueError(msg)

    normalized = normalize_markdown(text)
    if not normalized:
        return []
    if len(normalized) <= size:
        return [normalized]

    chunks: list[str] = []
    buffer = ""
    for sentence in split_sentences(normalized):
        if len(sentence) > size:
            if buffer.strip():
                chunks.append(buffer.strip())
            chunks.extend(_hard_split(sentence.strip(), size, overlap))
            buffer = ""
            continue
        if len(buffer) + len(sentence) <= size:
            buffer += sentence
            continue
        chunks.append(buffer.strip())
        carried = _overlap_tail(buffer, overlap) + sentence
        # 块大小上限是硬约束：带 overlap 会超限时宁可放弃重叠，也不产生超长块
        buffer = carried if len(carried) <= size else sentence
    if buffer.strip():
        chunks.append(buffer.strip())

    # 去掉过短尾块（可能是纯 overlap 残留），并去重
    cleaned: list[str] = []
    for chunk in chunks:
        stripped = chunk.strip()
        if not stripped:
            continue
        if cleaned and stripped == cleaned[-1]:
            continue
        cleaned.append(stripped)
    return cleaned


@dataclass(frozen=True, slots=True)
class Section:
    path: str
    text: str


def split_sections(markdown: str, *, default_title: str = "") -> list[Section]:
    """按 ``##``/``###`` 切节，返回带层级路径的正文。"""
    normalized = normalize_markdown(markdown)
    if not normalized:
        return []

    sections: list[Section] = []
    heading_stack: list[tuple[int, str]] = []
    buffer: list[str] = []

    def flush() -> None:
        body = "\n".join(buffer).strip()
        path = " > ".join(name for _, name in heading_stack) or default_title
        if body:
            sections.append(Section(path=path, text=body))
        buffer.clear()

    for line in normalized.split("\n"):
        if _TITLE_RE.match(line.strip()):
            # 一级标题是文档标题，不作为正文块
            flush()
            heading_stack = []
            continue
        heading = _HEADING_RE.match(line)
        if heading:
            flush()
            level = len(heading.group(1))
            name = heading.group(2).strip()
            heading_stack = [item for item in heading_stack if item[0] < level]
            heading_stack.append((level, name))
            continue
        buffer.append(line)
    flush()

    if not sections and default_title:
        return [Section(path=default_title, text=normalized)]
    return sections


def extract_title(markdown: str, *, fallback: str = "") -> str:
    """取 ``# `` 一级标题作为文档标题。"""
    for line in markdown.splitlines():
        match = _TITLE_RE.match(line.strip())
        if match:
            return match.group(1).strip()
    return fallback


def chunk_document(text: str, *, doc_id: str, title: str, version: str) -> list[Chunk]:
    """整篇文档 → 有序 Chunk 列表（含节路径与版本号）。"""
    resolved_title = title or extract_title(text, fallback=doc_id)
    sections = split_sections(text, default_title=resolved_title)
    chunks: list[Chunk] = []
    for section in sections:
        for body in chunk_text(section.text):
            chunks.append(
                Chunk(
                    doc_id=doc_id,
                    title=resolved_title,
                    section=section.path,
                    version=version,
                    text=body,
                    index=len(chunks),
                    metadata={"chars": str(len(body))},
                )
            )
    return chunks


@dataclass(frozen=True, slots=True)
class ParsedDocument:
    doc_id: str
    title: str
    source_path: str
    chunks: list[Chunk]


def load_markdown_file(path: Path, *, version: str) -> ParsedDocument:
    """读取单个 Markdown 语料文件并分块；``doc_id`` 取文件名（不含扩展名）。"""
    raw = path.read_text(encoding="utf-8")
    doc_id = path.stem
    title = extract_title(raw, fallback=doc_id)
    return ParsedDocument(
        doc_id=doc_id,
        title=title,
        source_path=str(path),
        chunks=chunk_document(raw, doc_id=doc_id, title=title, version=version),
    )


def load_corpus(directory: Path, *, version: str) -> list[ParsedDocument]:
    """加载目录下全部 ``*.md``；按文件名排序保证重建顺序稳定。"""
    if not directory.exists():
        return []
    return [
        load_markdown_file(path, version=version)
        for path in sorted(directory.glob("*.md"))
        if path.is_file()
    ]
