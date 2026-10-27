"""混合召回管线（Spec §7.2）：向量 Top20 + 关键词 Top20 → RRF Top10 → Rerank Top5 → 三档判定。

设计要点：

1. **降级链**：向量失败 → 关键词单通道；两路都失败 → 低置信兜底（绝不编造）；
2. **阈值只认向量/重排分**：BM25/ngram 分数没有跨 query 的绝对意义，不用于三档判定；
3. **RRF 复用 M0 已验证的排序语义**（k=60），但这里要保留正文与出处，因此自实现融合；
4. **Reranker 可关**（`RERANK_PROVIDER=none`）：CI/离线默认关闭，staging 开 LLM 重排。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from app.common.config import Settings
from app.common.logging import get_logger, log_warning
from app.llm.base import ChatModel, EmbeddingModel
from app.llm.types import ChatMessage, LLMError
from app.rag.cache import rag_cache_key
from app.rag.chroma import VectorHit, VectorIndex
from app.rag.errors import RetrievalError
from app.rag.keyword import KeywordHit, KeywordIndex, normalize_scores
from app.rag.retriever import ConfidenceBand, classify_confidence, reciprocal_rank_fusion

logger = get_logger("app.rag.pipeline")


@dataclass(frozen=True, slots=True)
class RetrievalHit:
    chunk_id: str
    doc_id: str
    title: str
    section: str
    content: str
    score: float
    vector_score: float | None = None
    keyword_score: float | None = None
    fused_score: float = 0.0
    vector_rank: int | None = None
    keyword_rank: int | None = None
    rerank_score: float | None = None

    def as_citation(self, citation_id: int) -> dict[str, object]:
        return {
            "id": citation_id,
            "doc_id": self.doc_id,
            "title": self.title,
            "section": self.section,
            "snippet": self.content[:200],
            "score": round(self.score, 4),
        }


@dataclass(frozen=True, slots=True)
class RetrievalResult:
    query: str
    index_version: str
    hits: list[RetrievalHit]
    band: ConfidenceBand
    degraded: list[str] = field(default_factory=list)
    vector_ok: bool = True
    keyword_ok: bool = True
    cached: bool = False

    def to_dict(self) -> dict[str, object]:
        return {
            "query": self.query,
            "index_version": self.index_version,
            "band": str(self.band),
            "degraded": list(self.degraded),
            "vector_ok": self.vector_ok,
            "keyword_ok": self.keyword_ok,
            "hits": [
                {
                    "chunk_id": hit.chunk_id,
                    "doc_id": hit.doc_id,
                    "title": hit.title,
                    "section": hit.section,
                    "content": hit.content,
                    "score": hit.score,
                    "vector_score": hit.vector_score,
                    "keyword_score": hit.keyword_score,
                    "fused_score": hit.fused_score,
                    "vector_rank": hit.vector_rank,
                    "keyword_rank": hit.keyword_rank,
                    "rerank_score": hit.rerank_score,
                }
                for hit in self.hits
            ],
        }

    @classmethod
    def from_dict(cls, payload: dict[str, object]) -> RetrievalResult:
        raw_hits_value = payload.get("hits")
        raw_hits = raw_hits_value if isinstance(raw_hits_value, list) else []
        hits = [
            RetrievalHit(
                chunk_id=str(item["chunk_id"]),
                doc_id=str(item["doc_id"]),
                title=str(item.get("title", "")),
                section=str(item.get("section", "")),
                content=str(item.get("content", "")),
                score=float(item.get("score", 0.0)),
                vector_score=_optional_float(item.get("vector_score")),
                keyword_score=_optional_float(item.get("keyword_score")),
                fused_score=float(item.get("fused_score", 0.0)),
                vector_rank=_optional_int(item.get("vector_rank")),
                keyword_rank=_optional_int(item.get("keyword_rank")),
                rerank_score=_optional_float(item.get("rerank_score")),
            )
            for item in raw_hits
            if isinstance(item, dict)
        ]
        return cls(
            query=str(payload.get("query", "")),
            index_version=str(payload.get("index_version", "")),
            hits=hits,
            band=ConfidenceBand(str(payload.get("band", "fallback"))),
            degraded=_as_str_list(payload.get("degraded")),
            vector_ok=bool(payload.get("vector_ok", True)),
            keyword_ok=bool(payload.get("keyword_ok", True)),
            cached=True,
        )

    @property
    def top1_score(self) -> float:
        return self.hits[0].score if self.hits else 0.0

    @property
    def empty(self) -> bool:
        return not self.hits


@runtime_checkable
class RetrievalCache(Protocol):
    async def get(self, key: str) -> object | None: ...

    async def set(self, key: str, value: object) -> None: ...


@runtime_checkable
class Reranker(Protocol):
    async def rerank(
        self, query: str, hits: list[RetrievalHit], *, top_k: int
    ) -> list[RetrievalHit]: ...


class NoopReranker:
    """关闭重排：RRF 只决定"谁进入 Top-K"，最终排序按向量分（判定口径与之一致）。

    为什么不直接保留 RRF 顺序：RRF 是名次融合，数值没有概率意义；
    若用它排序，会出现"向量分更高但关键词名次靠后"的资料排在下面，
    导致三档判定用到的 top1 分与真实最佳证据不一致。
    """

    async def rerank(
        self,
        query: str,  # noqa: ARG002 - 不重排，保持协议签名
        hits: list[RetrievalHit],
        *,
        top_k: int,
    ) -> list[RetrievalHit]:
        ordered = sorted(hits, key=lambda hit: (hit.score, hit.fused_score), reverse=True)
        return ordered[:top_k]


class LlmReranker:
    """用 LLM 对 Top10 打 0–1 分并重排；失败/超时回退融合序（不阻塞整条链路）。"""

    def __init__(self, chat: ChatModel, *, max_excerpt_chars: int = 260) -> None:
        self._chat = chat
        self._max_chars = max_excerpt_chars

    async def rerank(
        self, query: str, hits: list[RetrievalHit], *, top_k: int
    ) -> list[RetrievalHit]:
        if len(hits) <= 1:
            return hits[:top_k]
        passages = []
        for index, hit in enumerate(hits, start=1):
            excerpt = hit.content[: self._max_chars].replace("\n", " ")
            passages.append(f"[{index}] {hit.title} / {hit.section}：{excerpt}")
        messages = [
            ChatMessage(
                role="system",
                content=(
                    "你是检索重排器。判断每段资料与用户问题的相关度，输出 JSON："
                    '{"scores": [{"id": 1, "score": 0.0}]}，score 取 0 到 1。'
                    "只输出 JSON，不要解释。"
                ),
            ),
            ChatMessage(
                role="user",
                content="用户问题：" + query + "\n资料：\n" + "\n".join(passages),
            ),
        ]
        try:
            result = await self._chat.complete(
                messages, json_mode=True, max_tokens=600, tag="rerank"
            )
            payload = json.loads(result.content)
            raw_scores = payload.get("scores", [])
        except (LLMError, json.JSONDecodeError, ValueError) as exc:
            log_warning(logger, "rerank.fallback", reason=type(exc).__name__)
            return hits[:top_k]

        score_map: dict[int, float] = {}
        for item in raw_scores if isinstance(raw_scores, list) else []:
            if not isinstance(item, dict):
                continue
            try:
                identifier = int(item.get("id", 0))
                score = float(item.get("score", 0.0))
            except (TypeError, ValueError):
                continue
            score_map[identifier] = min(max(score, 0.0), 1.0)

        reranked: list[RetrievalHit] = []
        for index, hit in enumerate(hits, start=1):
            ranked = score_map.get(index)
            if ranked is None:
                reranked.append(hit)
                continue
            reranked.append(
                RetrievalHit(
                    chunk_id=hit.chunk_id,
                    doc_id=hit.doc_id,
                    title=hit.title,
                    section=hit.section,
                    content=hit.content,
                    score=ranked,
                    vector_score=hit.vector_score,
                    keyword_score=hit.keyword_score,
                    fused_score=hit.fused_score,
                    vector_rank=hit.vector_rank,
                    keyword_rank=hit.keyword_rank,
                    rerank_score=ranked,
                )
            )
        reranked.sort(key=lambda item: item.score, reverse=True)
        return reranked[:top_k]


def _as_str_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item) for item in value]


def _optional_float(value: object) -> float | None:
    if isinstance(value, bool):
        return float(value)
    if isinstance(value, (int, float, str)):
        try:
            return float(value)
        except (TypeError, ValueError):
            return None
    return None


def _optional_int(value: object) -> int | None:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.lstrip("-").isdigit():
        return int(value)
    if isinstance(value, float):
        return int(value)
    return None


def fuse_hits(
    vector_hits: list[VectorHit],
    keyword_hits: list[KeywordHit],
    *,
    rrf_k: int,
    top_n: int,
) -> list[RetrievalHit]:
    """RRF 融合两路召回，保留正文与出处，并记录各自名次。"""
    vector_by_id = {hit.chunk_id: hit for hit in vector_hits}
    keyword_by_id = {hit.chunk_id: hit for hit in keyword_hits}
    fused = reciprocal_rank_fusion(
        [hit.chunk_id for hit in vector_hits],
        [hit.chunk_id for hit in keyword_hits],
        k=rrf_k,
        top_n=top_n,
    )
    keyword_normalized = {hit.chunk_id: hit.score for hit in normalize_scores(keyword_hits)}

    results: list[RetrievalHit] = []
    for item in fused:
        vector_hit = vector_by_id.get(item.doc_id)
        keyword_hit = keyword_by_id.get(item.doc_id)
        if vector_hit is not None:
            doc_id = vector_hit.doc_id or (keyword_hit.doc_id if keyword_hit else "")
            title = vector_hit.title or (keyword_hit.title if keyword_hit else "")
            section = vector_hit.section or (keyword_hit.section if keyword_hit else "")
            content = vector_hit.content or (keyword_hit.content if keyword_hit else "")
        elif keyword_hit is not None:
            doc_id, title, section, content = (
                keyword_hit.doc_id,
                keyword_hit.title,
                keyword_hit.section,
                keyword_hit.content,
            )
        else:  # pragma: no cover - 融合结果必然来自某一路
            continue
        vector_score = vector_hit.score if vector_hit else None
        keyword_score = keyword_normalized.get(item.doc_id) if keyword_hit else None
        # 三档判定需要绝对分数：优先向量分；只有关键词时用归一化分（会附带"可能不完整"提示）
        effective = vector_score if vector_score is not None else (keyword_score or 0.0) * 0.6
        results.append(
            RetrievalHit(
                chunk_id=item.doc_id,
                doc_id=doc_id,
                title=title,
                section=section,
                content=content,
                score=effective,
                vector_score=vector_score,
                keyword_score=keyword_score,
                fused_score=item.score,
                vector_rank=item.vector_rank,
                keyword_rank=item.keyword_rank,
            )
        )
    return results


class HybridRetriever:
    """生产检索器：两路召回 + RRF + 可选重排 + 三档判定。"""

    def __init__(
        self,
        *,
        embedding: EmbeddingModel,
        vector_index: VectorIndex,
        keyword_index: KeywordIndex,
        settings: Settings,
        reranker: Reranker | None = None,
        cache: RetrievalCache | None = None,
    ) -> None:
        self._embedding = embedding
        self._vector = vector_index
        self._keyword = keyword_index
        self._settings = settings
        self._reranker: Reranker = reranker or NoopReranker()
        self._cache = cache

    async def retrieve(
        self,
        query: str,
        *,
        index_version: str,
        collection_id: str | None = None,
        top_k: int | None = None,
    ) -> RetrievalResult:
        cache_key = rag_cache_key(index_version, query)
        if self._cache is not None:
            cached = await self._cache.get(cache_key)
            if isinstance(cached, dict):
                return RetrievalResult.from_dict(cached)

        degraded: list[str] = []
        vector_hits: list[VectorHit] = []
        keyword_hits: list[KeywordHit] = []
        vector_ok = True
        keyword_ok = True

        # ---- 向量通道 ----
        try:
            vectors = await self._embedding.embed([query])
            if collection_id:
                vector_hits = await self._vector.search(
                    collection_id, vectors[0], top_k=self._settings.rag_top_k_vector
                )
            else:
                vector_ok = False
                degraded.append("vector:no_collection")
        except (RetrievalError, LLMError) as exc:
            vector_ok = False
            degraded.append(f"vector:{type(exc).__name__}")
            log_warning(logger, "retrieval.vector.failed", reason=type(exc).__name__)

        # ---- 关键词通道 ----
        try:
            keyword_hits = await self._keyword.search(
                query, index_version=index_version, top_k=self._settings.rag_top_k_keyword
            )
        except Exception as exc:  # 关键词失败必须可降级
            keyword_ok = False
            degraded.append(f"keyword:{type(exc).__name__}")
            log_warning(logger, "retrieval.keyword.failed", reason=type(exc).__name__)

        fused = fuse_hits(
            vector_hits,
            keyword_hits,
            rrf_k=self._settings.rag_rrf_k,
            top_n=self._settings.rag_top_k_fused,
        )
        final_k = top_k or self._settings.rag_top_k_final
        reranked = await self._reranker.rerank(query, fused, top_k=final_k)

        if vector_hits:
            band = classify_confidence(
                reranked[0].score if reranked else 0.0,
                high=self._settings.rag_top1_threshold,
                low=self._settings.rag_low_threshold,
            )
        elif reranked:
            # 只有关键词命中 → 可以作答但必须提示"可能不完整"
            band = ConfidenceBand.ANSWER_WITH_CAVEAT
            degraded.append("vector_only_keyword")
        else:
            band = ConfidenceBand.FALLBACK

        logger.info(
            "retrieval.done",
            extra={
                "index_version": index_version,
                "vector_hits": len(vector_hits),
                "keyword_hits": len(keyword_hits),
                "final_hits": len(reranked),
                "band": str(band),
                "degraded": degraded,
            },
        )
        result = RetrievalResult(
            query=query,
            index_version=index_version,
            hits=reranked,
            band=band,
            degraded=degraded,
            vector_ok=vector_ok,
            keyword_ok=keyword_ok,
        )
        if self._cache is not None and result.hits:
            await self._cache.set(cache_key, result.to_dict())
        return result
