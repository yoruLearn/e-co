"""答案级缓存测试：相同问题第二次命中缓存，不再调用生成模型。"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

from langgraph.checkpoint.memory import InMemorySaver

from app.agent.graph import build_graph
from app.agent.runtime import AgentRuntime
from app.agent.state import initial_state
from app.cache.two_level import TwoLevelCache
from app.common.config import REPO_ROOT, Settings
from app.llm.embeddings import HashEmbedding
from app.llm.types import ChatDelta, ChatMessage, ChatResult, Usage
from app.prompt.registry import PromptRegistry, SeedPromptSource
from app.rag.answerability import AlwaysAnswerableChecker
from app.rag.pipeline import RetrievalHit, RetrievalResult
from app.rag.retriever import ConfidenceBand
from app.session.store import SessionStore

SEED_PROMPTS = REPO_ROOT / "seeds" / "prompts.yaml"


class FakeRedis:
    def __init__(self) -> None:
        self.data: dict[str, Any] = {}

    async def get(self, key: str) -> Any:
        return self.data.get(key)

    async def set(self, key: str, value: str, ex: int | None = None) -> bool:
        self.data[key] = value
        return True

    async def delete(self, *keys: str) -> int:
        return sum(1 for key in keys if self.data.pop(key, None) is not None)

    async def scan(self, *, cursor: int = 0, match: str | None = None, count: int = 200):
        return 0, list(self.data)


class FakeChat:
    name = "fake"

    def __init__(self) -> None:
        self.stream_calls = 0

    async def complete(self, messages: list[ChatMessage], **kwargs: Any) -> ChatResult:
        if kwargs.get("json_mode"):
            return ChatResult(content=json.dumps({"intent": "qa", "confidence": 0.9}), model="fake")
        return ChatResult(content="改写后的问题", model="fake")

    async def stream(self, messages: list[ChatMessage], **kwargs: Any) -> AsyncIterator[ChatDelta]:
        self.stream_calls += 1
        yield ChatDelta(content="退款 1 个工作日到账[[1]]。")
        yield ChatDelta(finish_reason="stop", usage=Usage(total_tokens=20))

    async def aclose(self) -> None:
        return None


class FakeRetriever:
    def __init__(self) -> None:
        self.calls = 0

    async def retrieve(
        self,
        query: str,
        *,
        index_version: str,
        collection_id: str | None,
        top_k: int | None = None,
    ) -> RetrievalResult:
        self.calls += 1
        return RetrievalResult(
            query=query,
            index_version=index_version,
            hits=[
                RetrievalHit(
                    chunk_id="p#0",
                    doc_id="p",
                    title="政策",
                    section="退款时效",
                    content="余额 1 个工作日到账。",
                    score=0.9,
                )
            ],
            band=ConfidenceBand.ANSWER,
        )


def make_settings() -> Settings:
    return Settings(
        _env_file=None,
        app_env="test",
        log_level="WARNING",
        log_format="console",
        prompt_seed_path=str(SEED_PROMPTS),
        rag_offline=True,
    )


async def test_second_identical_question_hits_answer_cache() -> None:
    settings = make_settings()
    chat = FakeChat()
    retriever = FakeRetriever()
    runtime = AgentRuntime(
        settings=settings,
        chat=chat,  # type: ignore[arg-type]
        embedding=HashEmbedding(dim=8),
        prompts=PromptRegistry([SeedPromptSource(SEED_PROMPTS)]),
        retriever=retriever,  # type: ignore[arg-type]
        vector_index=None,
        checkpointer=InMemorySaver(),
        session_store=SessionStore(None),  # type: ignore[arg-type]
        clients=None,
        answerability=AlwaysAnswerableChecker(),
        cache=TwoLevelCache(FakeRedis()),  # type: ignore[arg-type]
    )
    runtime.graph = build_graph(runtime)

    async def run(thread: str) -> dict[str, Any]:
        state = initial_state(
            trace_id=f"tr_{thread}",
            session_id=thread,
            user_id="u",
            user_query="退款多久到账",
        )
        await runtime.graph.ainvoke(state, {"configurable": {"thread_id": thread}})
        snapshot = await runtime.graph.aget_state({"configurable": {"thread_id": thread}})
        return dict(snapshot.values or {})

    first = await run("cache-1")
    second = await run("cache-2")

    assert chat.stream_calls == 1, "第二次应命中答案缓存，不再调用生成模型"
    assert second["answer_stream"] == first["answer_stream"]
    assert "answer:cache" in second["degraded"]
    assert "[[1]]" in second["answer_stream"]
