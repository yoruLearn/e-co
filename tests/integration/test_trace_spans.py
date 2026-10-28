"""图节点 span 采集测试：跑一遍真实图（Fake 模型/检索器），断言每个节点都有 span。"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

from langgraph.checkpoint.memory import InMemorySaver

from app.agent.graph import build_graph
from app.agent.runtime import AgentRuntime
from app.agent.state import initial_state
from app.common.config import REPO_ROOT, Settings
from app.llm.embeddings import HashEmbedding
from app.llm.types import ChatDelta, ChatMessage, ChatResult, Usage
from app.prompt.registry import PromptRegistry, SeedPromptSource
from app.rag.pipeline import RetrievalHit, RetrievalResult
from app.rag.retriever import ConfidenceBand

SEED_PROMPTS = REPO_ROOT / "seeds" / "prompts.yaml"


class FakeChat:
    name = "fake"

    async def complete(self, messages: list[ChatMessage], **kwargs: Any) -> ChatResult:
        if kwargs.get("json_mode"):
            return ChatResult(content=json.dumps({"intent": "qa", "confidence": 0.9}), model="fake")
        return ChatResult(content="改写后的问题", model="fake")

    async def stream(self, messages: list[ChatMessage], **kwargs: Any) -> AsyncIterator[ChatDelta]:
        yield ChatDelta(content="退款 1 个工作日到账[[1]]。")
        yield ChatDelta(finish_reason="stop", usage=Usage(total_tokens=12))

    async def aclose(self) -> None:
        return None


class FakeRetriever:
    async def retrieve(
        self, query: str, *, index_version: str, collection_id: str | None, top_k: int | None = None
    ) -> RetrievalResult:
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


class RecordingTrace:
    def __init__(self) -> None:
        self.spans: list[dict[str, Any]] = []

    async def record(self, **kwargs: Any) -> None:
        self.spans.append(kwargs)

    async def fetch(self, trace_id: str) -> list[dict[str, Any]]:
        return []


def make_settings() -> Settings:
    return Settings(
        _env_file=None,
        app_env="test",
        log_level="WARNING",
        log_format="console",
        shutdown_drain_seconds=0,
        database_url="mysql+aiomysql://u:p@127.0.0.1:1/x",
        redis_url="redis://127.0.0.1:1/0",
        chroma_url="http://127.0.0.1:1",
        rag_offline=True,
        prompt_seed_path=str(SEED_PROMPTS),
        checkpointer_backend="memory",
    )


async def test_graph_records_span_per_node() -> None:
    settings = make_settings()
    trace = RecordingTrace()
    runtime = AgentRuntime(
        settings=settings,
        chat=FakeChat(),  # type: ignore[arg-type]
        embedding=HashEmbedding(dim=8),
        prompts=PromptRegistry([SeedPromptSource(SEED_PROMPTS)]),
        retriever=FakeRetriever(),  # type: ignore[arg-type]
        vector_index=None,
        checkpointer=InMemorySaver(),
        session_store=__import__("app.session.store", fromlist=["SessionStore"]).SessionStore(None),
        clients=None,
        trace=trace,  # type: ignore[arg-type]
    )
    runtime.graph = build_graph(runtime)

    state = initial_state(trace_id="tr_test_span", session_id="s-span", user_id="u")
    await runtime.graph.ainvoke(state, {"configurable": {"thread_id": "s-span"}})

    nodes = [span["node"] for span in trace.spans]
    assert "router" in nodes
    assert "rewrite" in nodes
    assert "retrieve" in nodes
    assert "answer" in nodes
    assert "sse_out" in nodes
    assert all(span["trace_id"] == "tr_test_span" for span in trace.spans)
    assert all(span["duration_ms"] >= 0 for span in trace.spans)
