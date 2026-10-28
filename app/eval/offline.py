"""离线评测运行时：不依赖 MySQL/Chroma，用内存索引 + 当前配置的 embedding/模型。

用途：
- 本机没有 Docker、只想快速看"检索+作答"质量时，跑 ``eval cli --offline``；
- CI 只跑执行器/断言的单测，不跑这里（避免下载模型与真实模型调用）。

⚠️ 离线模式用的是示例语料 + 内存索引，指标只代表**管道与提示词**质量，
不能替代 staging（真实 MySQL + Chroma + 真实语料）的验收数字。
"""

from __future__ import annotations

from pathlib import Path

from langgraph.checkpoint.memory import InMemorySaver

from app.agent.graph import build_graph
from app.agent.runtime import AgentRuntime
from app.common.config import Settings
from app.llm.factory import build_chat_model, build_embedding
from app.prompt.registry import PromptRegistry, SeedPromptSource
from app.rag.answerability import AlwaysAnswerableChecker, LlmAnswerabilityChecker
from app.rag.chroma import InMemoryVectorIndex
from app.rag.chunker import collection_name, load_corpus
from app.rag.keyword import KeywordDocument, MemoryBM25KeywordIndex, chunk_id_of
from app.rag.pipeline import HybridRetriever, NoopReranker
from app.session.store import SessionState


class NullSessionStore:
    """离线评测每条用例都是独立会话，不需要跨轮记忆。"""

    async def load(self, session_id: str) -> SessionState:
        return SessionState()

    async def append(
        self, session_id: str, *, user_message: str, assistant_message: str
    ) -> SessionState:
        return SessionState(turns=[{"role": "user", "content": user_message}])

    async def clear(self, session_id: str) -> None:
        return None


async def build_offline_runtime(
    settings: Settings, *, corpus_dir: Path | None = None
) -> AgentRuntime:
    directory = corpus_dir or Path(settings.corpus_dir)
    version = settings.rag_index_version
    documents = load_corpus(directory, version=version)
    chunks = [chunk for document in documents for chunk in document.chunks]

    embedding = build_embedding(settings)
    vector_index = InMemoryVectorIndex()
    collection_id = await vector_index.create_collection(collection_name(version))
    if chunks:
        vectors = await embedding.embed([chunk.text for chunk in chunks])
        await vector_index.upsert(
            collection_id,
            ids=[chunk_id_of(chunk.doc_id, chunk.index) for chunk in chunks],
            embeddings=vectors,
            documents=[chunk.text for chunk in chunks],
            metadatas=[chunk.to_metadata() for chunk in chunks],
        )

    keyword_index = MemoryBM25KeywordIndex()
    keyword_index.add_documents(
        version,
        [
            KeywordDocument(
                chunk_id=chunk_id_of(chunk.doc_id, chunk.index),
                doc_id=chunk.doc_id,
                title=chunk.title,
                section=chunk.section,
                content=chunk.text,
            )
            for chunk in chunks
        ],
    )
    retriever = HybridRetriever(
        embedding=embedding,
        vector_index=vector_index,
        keyword_index=keyword_index,
        settings=settings,
        reranker=NoopReranker(),
        cache=None,
    )
    chat = build_chat_model(settings)
    prompts = PromptRegistry([SeedPromptSource(Path(settings.prompt_seed_path))])
    runtime = AgentRuntime(
        settings=settings,
        chat=chat,
        embedding=embedding,
        prompts=prompts,
        retriever=retriever,
        vector_index=vector_index,  # type: ignore[arg-type]
        checkpointer=InMemorySaver(),
        session_store=NullSessionStore(),  # type: ignore[arg-type]
        clients=None,
        answerability=(
            LlmAnswerabilityChecker(chat, prompts)
            if settings.rag_answerability_enabled
            else AlwaysAnswerableChecker()
        ),
    )
    runtime.graph = build_graph(runtime)
    return runtime
