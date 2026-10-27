"""关键词通道（Spec §7.2 的"关键词 Top20"）。

两种实现：

- ``MysqlKeywordIndex``：生产/联调用 MySQL 8 ``FULLTEXT ... WITH PARSER ngram``，
  中文按 2-gram 建索引，``MATCH ... AGAINST`` 打分；
- ``MemoryBM25KeywordIndex``：单测/离线评测用内存 BM25（k1=1.5, b=0.75），
  分词规则与 ngram 对齐（英文按词、中文按 bigram + 单字兜底）。

两个实现共用 ``chunk_id = "{doc_id}#{chunk_index}"``，保证与向量通道可以按 id 融合。
"""

from __future__ import annotations

import math
import re
from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from app.common.logging import get_logger
from app.rag.errors import RetrievalError

logger = get_logger("app.rag.keyword")

_WORD_RE = re.compile(r"[a-z0-9]+")
_CJK_RE = re.compile(r"[\u4e00-\u9fff]")
_NON_WORD_RE = re.compile(r"[^\w\u4e00-\u9fff]+", re.UNICODE)

BM25_K1 = 1.5
BM25_B = 0.75


def chunk_id_of(doc_id: str, chunk_index: int) -> str:
    return f"{doc_id}#{chunk_index}"


@dataclass(frozen=True, slots=True)
class KeywordHit:
    chunk_id: str
    doc_id: str
    title: str
    section: str
    content: str
    score: float


@dataclass(frozen=True, slots=True)
class KeywordDocument:
    """关键词索引的最小文档单元（与 ``rag_chunk`` 一行对应）。"""

    chunk_id: str
    doc_id: str
    title: str
    section: str
    content: str


@runtime_checkable
class KeywordIndex(Protocol):
    async def search(self, query: str, *, index_version: str, top_k: int) -> list[KeywordHit]: ...


def normalize_for_keyword(text: str) -> str:
    lowered = text.lower().strip()
    cleaned = _NON_WORD_RE.sub(" ", lowered)
    return re.sub(r"\s+", " ", cleaned).strip()


def tokenize(text: str) -> list[str]:
    """英文按词、中文按 2-gram + 单字（与 MySQL ngram parser 行为对齐）。"""
    normalized = normalize_for_keyword(text)
    if not normalized:
        return []
    tokens: list[str] = []
    for word in _WORD_RE.findall(normalized):
        tokens.append(word)
    compact = normalized.replace(" ", "")
    for index, char in enumerate(compact):
        if _CJK_RE.match(char):
            tokens.append(char)
            if index + 1 < len(compact) and _CJK_RE.match(compact[index + 1]):
                tokens.append(compact[index : index + 2])
    return tokens


class MemoryBM25KeywordIndex:
    """内存 BM25；按 index_version 隔离，切换版本即换数据集。"""

    def __init__(self) -> None:
        self._docs: dict[str, list[KeywordDocument]] = {}
        self._tokens: dict[str, list[list[str]]] = {}
        self._df: dict[str, Counter[str]] = {}
        self._avgdl: dict[str, float] = {}

    def add_documents(self, index_version: str, documents: Iterable[KeywordDocument]) -> None:
        docs = list(documents)
        self._docs[index_version] = docs
        tokenized = [tokenize(f"{doc.title} {doc.section} {doc.content}") for doc in docs]
        self._tokens[index_version] = tokenized
        df: Counter[str] = Counter()
        for tokens in tokenized:
            df.update(set(tokens))
        self._df[index_version] = df
        total = sum(len(tokens) for tokens in tokenized)
        self._avgdl[index_version] = (total / len(tokenized)) if tokenized else 0.0
        logger.info(
            "keyword.index.built",
            extra={"index_version": index_version, "docs": len(docs), "terms": len(df)},
        )

    def size(self, index_version: str) -> int:
        return len(self._docs.get(index_version, []))

    async def search(self, query: str, *, index_version: str, top_k: int) -> list[KeywordHit]:
        docs = self._docs.get(index_version, [])
        if not docs:
            return []
        query_tokens = tokenize(query)
        if not query_tokens:
            return []
        tokenized = self._tokens[index_version]
        df = self._df[index_version]
        avgdl = self._avgdl[index_version] or 1.0
        total_docs = len(docs)

        scored: list[tuple[float, int]] = []
        for index, tokens in enumerate(tokenized):
            counts = Counter(tokens)
            length = len(tokens) or 1
            score = 0.0
            for token in query_tokens:
                frequency = counts.get(token, 0)
                if not frequency:
                    continue
                idf = math.log(1 + (total_docs - df.get(token, 0) + 0.5) / (df.get(token, 0) + 0.5))
                denominator = frequency + BM25_K1 * (1 - BM25_B + BM25_B * length / avgdl)
                score += idf * frequency * (BM25_K1 + 1) / denominator
            if score > 0:
                scored.append((score, index))

        scored.sort(key=lambda item: item[0], reverse=True)
        hits: list[KeywordHit] = []
        for score, index in scored[:top_k]:
            doc = docs[index]
            hits.append(
                KeywordHit(
                    chunk_id=doc.chunk_id,
                    doc_id=doc.doc_id,
                    title=doc.title,
                    section=doc.section,
                    content=doc.content,
                    score=score,
                )
            )
        return hits


_MYSQL_SEARCH_SQL = text(
    """
    SELECT doc_id, chunk_index, title, section, content,
           MATCH(title, content) AGAINST (:query IN NATURAL LANGUAGE MODE) AS score
    FROM rag_chunk
    WHERE index_version = :index_version
      AND MATCH(title, content) AGAINST (:query IN NATURAL LANGUAGE MODE)
    ORDER BY score DESC
    LIMIT :top_k
    """
)


class MysqlKeywordIndex:
    """MySQL ngram 全文检索。"""

    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    async def search(self, query: str, *, index_version: str, top_k: int) -> list[KeywordHit]:
        if not normalize_for_keyword(query):
            return []
        try:
            async with self._engine.connect() as conn:
                result = await conn.execute(
                    _MYSQL_SEARCH_SQL,
                    {"query": query, "index_version": index_version, "top_k": top_k},
                )
                rows = result.fetchall()
        except Exception as exc:
            raise RetrievalError(
                f"MySQL 关键词检索失败：{type(exc).__name__}",
                detail={"index_version": index_version},
            ) from exc
        return [
            KeywordHit(
                chunk_id=chunk_id_of(str(row[0]), int(row[1])),
                doc_id=str(row[0]),
                title=str(row[2]),
                section=str(row[3]),
                content=str(row[4]),
                score=float(row[5]),
            )
            for row in rows
        ]


class EmptyKeywordIndex:
    """未配置关键词通道时的空实现（保证管线可运行）。"""

    async def search(
        self,
        query: str,  # noqa: ARG002
        *,
        index_version: str,  # noqa: ARG002
        top_k: int,  # noqa: ARG002
    ) -> list[KeywordHit]:
        return []


def normalize_scores(hits: Sequence[KeywordHit]) -> list[KeywordHit]:
    """把 BM25/ngram 原始分数按本轮最高分归一到 0–1（仅用于展示与排序参考）。

    注意：**阈值判定不用这个分数**——它没有跨 query 的绝对意义，
    Spec 的三档阈值基于向量/重排分数。
    """
    if not hits:
        return []
    best = max(hit.score for hit in hits) or 1.0
    return [
        KeywordHit(
            chunk_id=hit.chunk_id,
            doc_id=hit.doc_id,
            title=hit.title,
            section=hit.section,
            content=hit.content,
            score=hit.score / best,
        )
        for hit in hits
    ]
