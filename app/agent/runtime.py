"""Agent 运行时：把 provider / 检索 / Prompt / checkpointer 组装成可执行的图。

设计约束：

- **启动不连依赖**：Chroma/MySQL/Redis 都是懒连接；`/readyz` 负责健康判定；
- **依赖可替换**：测试通过构造 ``AgentRuntime`` 注入 Fake ChatModel / Fake 检索器，
  不需要起真实服务；
- **降级优先**：索引版本、collection、会话记忆任何一项失败都只降级（关键词通道/无记忆），
  不让对话直接 500。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select

from app.agent.checkpointer import build_checkpointer, close_checkpointer, init_checkpointer
from app.agent.graph import build_graph
from app.api.sse_buffer import SseBuffer
from app.cache.two_level import TwoLevelCache
from app.common.clients import AppClients
from app.common.config import Settings
from app.common.logging import get_logger, log_warning
from app.db.models import IndexVersion
from app.llm.base import ChatModel, EmbeddingModel
from app.llm.factory import build_chat_model, build_embedding
from app.prompt.registry import PromptRegistry
from app.rag.answerability import (
    AlwaysAnswerableChecker,
    AnswerabilityChecker,
    LlmAnswerabilityChecker,
)
from app.rag.chroma import ChromaVectorIndex, VectorHit
from app.rag.chunker import collection_name
from app.rag.keyword import EmptyKeywordIndex, KeywordIndex, MysqlKeywordIndex
from app.rag.pipeline import HybridRetriever, LlmReranker, NoopReranker
from app.services.aftersales import AfterSalesService
from app.services.confirm import ConfirmTokenService
from app.services.tool_audit import ToolAuditRecorder
from app.services.trace_store import NullTraceRecorder, TraceRecorder
from app.session.store import SessionStore
from app.session.summarize import SessionSummarizer
from app.tools.aftersales import AfterSalesTools
from app.tools.orders import MockOrderProvider, OrderTools

logger = get_logger("app.agent.runtime")


@dataclass(slots=True)
class AgentRuntime:
    settings: Settings
    chat: ChatModel
    embedding: EmbeddingModel
    prompts: PromptRegistry
    retriever: HybridRetriever
    vector_index: ChromaVectorIndex | None
    checkpointer: Any
    session_store: SessionStore
    graph: Any = None
    clients: AppClients | None = None
    sse_buffer: SseBuffer = field(default_factory=lambda: SseBuffer(None))
    cache: TwoLevelCache | None = None
    trace: TraceRecorder | NullTraceRecorder = field(default_factory=NullTraceRecorder)
    answerability: AnswerabilityChecker = field(default_factory=AlwaysAnswerableChecker)
    order_tools: Any = None
    confirm: Any = None
    aftersales: Any = None
    aftersales_tools: Any = None
    _collection_cache: dict[str, str] = field(default_factory=dict)
    _last_index_version: str | None = None

    async def active_index_version(self) -> str:
        """取 active 版本；DB 不可用时回落到配置里的默认版本（离线/测试）。"""
        if self.clients is not None:
            try:
                async with self.clients.session_factory() as session:
                    row = await session.scalar(
                        select(IndexVersion)
                        .where(IndexVersion.status == "active")
                        .order_by(IndexVersion.id.desc())
                    )
                if row is not None:
                    await self._invalidate_on_version_change(row.version)
                    return row.version
            except Exception as exc:
                log_warning(logger, "runtime.index_version.failed", reason=type(exc).__name__)
        version = self.settings.rag_index_version
        await self._invalidate_on_version_change(version)
        return version

    async def _invalidate_on_version_change(self, version: str) -> None:
        """索引版本切换时整组检索缓存失效（FR-CACHE-03）。"""
        previous = self._last_index_version
        if previous is None or previous == version:
            self._last_index_version = version
            return
        if self.cache is not None:
            removed = await self.cache.invalidate_prefix(f"rag:{previous}:")
            log_warning(
                logger,
                "runtime.index_version.switched",
                previous=previous,
                current=version,
                cache_removed=removed,
            )
        self._last_index_version = version

    async def collection_id(self, version: str) -> str | None:
        """取/建 collection；Chroma 不可用返回 None（检索器会自动只用关键词通道）。"""
        if self.vector_index is None:
            return None
        cached = self._collection_cache.get(version)
        if cached:
            return cached
        try:
            collection_id = await self.vector_index.create_collection(collection_name(version))
        except Exception as exc:
            log_warning(logger, "runtime.collection.failed", reason=type(exc).__name__)
            return None
        self._collection_cache[version] = collection_id
        return collection_id

    async def close(self) -> None:
        await self.chat.aclose()
        if self.vector_index is not None:
            await self.vector_index.aclose()
        await close_checkpointer(self.checkpointer)


def build_agent_runtime(settings: Settings, clients: AppClients) -> AgentRuntime:
    chat = build_chat_model(settings)
    embedding = build_embedding(settings)
    vector_index: ChromaVectorIndex | None = None
    keyword_index: KeywordIndex
    if settings.rag_offline:
        vector_index = None
        keyword_index = EmptyKeywordIndex()
        log_warning(logger, "runtime.offline", reason="RAG_OFFLINE=1，检索不可用")
    else:
        vector_index = ChromaVectorIndex(settings.chroma_url)
        keyword_index = (
            MysqlKeywordIndex(clients.engine)
            if settings.keyword_backend == "mysql"
            else EmptyKeywordIndex()
        )

    reranker = LlmReranker(chat) if settings.rerank_provider == "llm" else NoopReranker()
    prompts = PromptRegistry.from_settings(settings, session_factory=clients.session_factory)
    cache = TwoLevelCache(
        clients.redis,
        local_ttl_seconds=60,
        redis_ttl_seconds=settings.rag_cache_ttl_sec,
    )
    retriever = HybridRetriever(
        embedding=embedding,
        vector_index=vector_index or _NullVectorIndex(),
        keyword_index=keyword_index,
        settings=settings,
        reranker=reranker,
        cache=cache,
    )
    order_tools = OrderTools(MockOrderProvider())
    aftersales_service = AfterSalesService(clients.session_factory, settings=settings)
    confirm_tokens = ConfirmTokenService(clients.redis)
    runtime = AgentRuntime(
        settings=settings,
        chat=chat,
        embedding=embedding,
        prompts=prompts,
        retriever=retriever,
        vector_index=vector_index,
        checkpointer=build_checkpointer(settings),
        session_store=SessionStore(
            clients.redis,
            ttl_seconds=settings.session_ttl_hours * 3600,
            window_turns=settings.session_window_turns,
            summarizer=SessionSummarizer(chat, prompts),
        ),
        clients=clients,
        sse_buffer=SseBuffer(
            clients.redis,
            ttl_seconds=settings.sse_buffer_ttl_sec,
            max_frames=settings.sse_buffer_max_frames,
        ),
        cache=cache,
        trace=TraceRecorder(clients.session_factory),
        answerability=(
            LlmAnswerabilityChecker(chat, prompts)
            if settings.rag_answerability_enabled
            else AlwaysAnswerableChecker()
        ),
        order_tools=order_tools,
        confirm=confirm_tokens,
        aftersales=aftersales_service,
        aftersales_tools=AfterSalesTools(
            aftersales_service,
            confirm_tokens,
            ToolAuditRecorder(clients.session_factory),
        ),
    )
    runtime.graph = build_graph(runtime)
    return runtime


async def init_agent_runtime(runtime: AgentRuntime) -> None:
    """Redis checkpointer 初始化；失败时**降级为内存**而不是让应用启动失败。"""
    try:
        await init_checkpointer(runtime.checkpointer)
    except Exception as exc:
        log_warning(logger, "runtime.checkpointer.fallback", reason=type(exc).__name__)
        from langgraph.checkpoint.memory import InMemorySaver

        runtime.checkpointer = InMemorySaver()
        runtime.graph = build_graph(runtime)


class _NullVectorIndex:
    """离线模式下占位：任何查询都返回空，触发关键词/兜底路径。"""

    async def create_collection(self, name: str) -> str:
        return f"offline-{name}"

    async def upsert(self, *args: object, **kwargs: object) -> None:
        return None

    async def search(
        self, collection_id: str, embedding: list[float], *, top_k: int
    ) -> list[VectorHit]:
        return []

    async def delete_collection(self, name: str) -> None:
        return None
