"""节点边界分支补充测试：改写兜底、兜底话术、缓存入口、生成失败、判定未知。"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

from langgraph.checkpoint.memory import InMemorySaver

from app.agent.nodes.core import (
    CHITCHAT_TEXT,
    NO_ANSWER_TEXT,
    PENDING_TEXT,
    answer_node,
    cache_probe_node,
    clarify_node,
    is_meaningful_rewrite,
    pending_node,
    reject_node,
    retrieve_node,
    rule_rewrite,
)
from app.agent.runtime import AgentRuntime
from app.agent.state import initial_state
from app.cache.two_level import TwoLevelCache
from app.common.config import REPO_ROOT, Settings
from app.llm.embeddings import HashEmbedding
from app.llm.types import ChatMessage, LLMError
from app.prompt.registry import PromptRegistry, SeedPromptSource
from app.rag.cache import answer_cache_key
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
        return 0

    async def scan(self, *, cursor: int = 0, match: str | None = None, count: int = 200):
        return 0, list(self.data)


class FailingChat:
    name = "failing"

    async def complete(self, messages: list[ChatMessage], **kwargs: Any) -> Any:
        raise LLMError("down")

    async def stream(self, messages: list[ChatMessage], **kwargs: Any) -> AsyncIterator[Any]:
        raise LLMError("down")
        yield  # pragma: no cover

    async def aclose(self) -> None:
        return None


def make_settings(**overrides: object) -> Settings:
    base: dict[str, object] = {
        "_env_file": None,
        "app_env": "test",
        "log_level": "WARNING",
        "log_format": "console",
        "rag_offline": True,
        "prompt_seed_path": str(SEED_PROMPTS),
    }
    base.update(overrides)
    return Settings(**base)


def build_runtime(
    settings: Settings,
    *,
    chat: Any = None,
    retriever: Any = None,
    checker: Any = None,
    cache: Any = None,
) -> AgentRuntime:
    return AgentRuntime(
        settings=settings,
        chat=chat or FailingChat(),  # type: ignore[arg-type]
        embedding=HashEmbedding(dim=8),
        prompts=PromptRegistry([SeedPromptSource(SEED_PROMPTS)]),
        retriever=retriever,  # type: ignore[arg-type]
        vector_index=None,
        checkpointer=InMemorySaver(),
        session_store=SessionStore(None),  # type: ignore[arg-type]
        clients=None,
        answerability=checker,  # type: ignore[arg-type]
        cache=cache,
    )


def test_rule_rewrite_uses_last_topic_for_pronoun() -> None:
    history = [{"role": "user", "content": "我的蓝牙耳机订单能退吗"}]
    assert rule_rewrite("那这一单呢", history) == "我的蓝牙耳机订单能退吗；那这一单呢"


def test_rule_rewrite_keeps_self_contained_question() -> None:
    history = [{"role": "user", "content": "退款多久到账"}]
    assert rule_rewrite("跨境订单被扣了怎么办", history) == "跨境订单被扣了怎么办"


def test_rule_rewrite_without_history() -> None:
    assert rule_rewrite("那这一单呢", []) == "那这一单呢"


def test_is_meaningful_rewrite_ignores_punctuation_only_changes() -> None:
    assert is_meaningful_rewrite("退款多久到账？", "退款多久到账") is False
    assert is_meaningful_rewrite("银行卡退款多久到账", "退款多久到账") is True
    assert is_meaningful_rewrite("", "退款多久到账") is False


async def test_clarify_reject_pending_texts() -> None:
    state = initial_state(trace_id="tr", session_id="s", user_id="u")
    clarify = await clarify_node(None, state)  # type: ignore[arg-type]
    reject = await reject_node(None, state)  # type: ignore[arg-type]
    pending = await pending_node(None, state)  # type: ignore[arg-type]
    assert clarify["answer_stream"] == NO_ANSWER_TEXT
    assert reject["answer_stream"] == CHITCHAT_TEXT
    assert pending["answer_stream"] == PENDING_TEXT


async def test_answer_node_llm_failure_returns_fallback() -> None:
    runtime = build_runtime(make_settings())
    state = initial_state(trace_id="tr", session_id="s", user_id="u")
    state["citations"] = [
        {"id": 1, "doc_id": "p", "title": "政策", "section": "退款", "snippet": "x"}
    ]
    state["context_blocks"] = ["[1] 政策 / 退款\n余额 1 个工作日到账"]
    update = await answer_node(runtime, state)
    assert update["answer_stream"] == NO_ANSWER_TEXT
    assert "band" in update and update["band"] == "fallback"
    assert any(item.startswith("answer:") for item in update["degraded"])


class StubChecker:
    def __init__(self, answer: bool | None) -> None:
        self._answer = answer

    async def is_answerable(self, *, question: str, passages: list[str]) -> bool | None:
        return self._answer


class CaveatRetriever:
    async def retrieve(
        self,
        query: str,
        *,
        index_version: str,
        collection_id: str | None,
        top_k: int | None = None,
    ) -> RetrievalResult:
        return RetrievalResult(
            query=query,
            index_version=index_version,
            hits=[
                RetrievalHit(
                    chunk_id="p#0",
                    doc_id="p",
                    title="政策",
                    section="退款",
                    content="退款时效说明",
                    score=0.6,
                )
            ],
            band=ConfidenceBand.ANSWER_WITH_CAVEAT,
        )


async def test_retrieve_node_unknown_answerability_keeps_band() -> None:
    runtime = build_runtime(make_settings(), retriever=CaveatRetriever(), checker=StubChecker(None))
    state = initial_state(trace_id="tr", session_id="s", user_id="u")
    state["user_query"] = "退款多久到账"
    update = await retrieve_node(runtime, state)
    assert update["band"] == "answer_with_caveat"
    assert "answerability:unknown" in update["degraded"]


async def test_cache_probe_hit_short_circuits() -> None:
    settings = make_settings()
    redis = FakeRedis()
    cache = TwoLevelCache(redis)  # type: ignore[arg-type]
    key = answer_cache_key(settings.rag_index_version, "退款多久到账")
    await cache.set(
        key,
        {
            "answer": "余额 1 个工作日到账[[1]]。",
            "citations": [
                {"id": 1, "doc_id": "p", "title": "政策", "section": "退款", "snippet": "x"}
            ],
        },
    )
    runtime = build_runtime(settings, cache=cache)
    state = initial_state(trace_id="tr", session_id="s", user_id="u")
    state["user_query"] = "退款多久到账"
    update = await cache_probe_node(runtime, state)
    assert update["cache_hit"] is True
    assert "[[1]]" in update["answer_stream"]
    assert "answer:cache" in update["degraded"]


async def test_cache_probe_miss_without_cache() -> None:
    runtime = build_runtime(make_settings(), cache=None)
    state = initial_state(trace_id="tr", session_id="s", user_id="u")
    state["user_query"] = "退款多久到账"
    update = await cache_probe_node(runtime, state)
    assert update == {"cache_hit": False}
