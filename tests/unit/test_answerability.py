"""可答性判定测试：判定器本身 + retrieve_node 的兜底接线。"""

from __future__ import annotations

import json
from typing import Any

from langgraph.checkpoint.memory import InMemorySaver

from app.agent.nodes.core import retrieve_node
from app.agent.runtime import AgentRuntime
from app.agent.state import initial_state
from app.common.config import REPO_ROOT, Settings
from app.llm.embeddings import HashEmbedding
from app.llm.types import ChatResult
from app.rag.answerability import LlmAnswerabilityChecker
from app.rag.pipeline import RetrievalHit, RetrievalResult
from app.rag.retriever import ConfidenceBand

SEED_PROMPTS = REPO_ROOT / "seeds" / "prompts.yaml"


class FakePrompts:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def render(self, key: str, **values: Any) -> str:
        self.calls.append({"key": key, **values})
        return f"{key}:{values.get('question', '')}"


class FakeChat:
    def __init__(self, payload: Any) -> None:
        self._payload = payload
        self.calls = 0

    async def complete(self, messages: Any, **kwargs: Any) -> ChatResult:
        self.calls += 1
        if isinstance(self._payload, Exception):
            raise self._payload
        content = self._payload if isinstance(self._payload, str) else json.dumps(self._payload)
        return ChatResult(content=content, model="fake")

    async def stream(self, messages: Any, **kwargs: Any) -> Any:
        raise NotImplementedError

    async def aclose(self) -> None:
        return None


async def test_checker_true_and_false() -> None:
    prompts = FakePrompts()
    assert (
        await LlmAnswerabilityChecker(FakeChat({"answerable": True}), prompts).is_answerable(  # type: ignore[arg-type]
            question="退款多久到账", passages=["余额 1 个工作日"]
        )
        is True
    )
    assert (
        await LlmAnswerabilityChecker(FakeChat({"answerable": False}), prompts).is_answerable(  # type: ignore[arg-type]
            question="海外仓维修怎么预约", passages=["退款时效"]
        )
        is False
    )


async def test_checker_parses_string_and_garbage() -> None:
    prompts = FakePrompts()
    assert (
        await LlmAnswerabilityChecker(FakeChat('{"answerable": "false"}'), prompts).is_answerable(  # type: ignore[arg-type]
            question="q", passages=["p"]
        )
        is False
    )
    assert (
        await LlmAnswerabilityChecker(FakeChat("not json"), prompts).is_answerable(  # type: ignore[arg-type]
            question="q", passages=["p"]
        )
        is None
    )


async def test_checker_fails_open_on_llm_error() -> None:
    from app.llm.types import LLMError

    checker = LlmAnswerabilityChecker(FakeChat(LLMError("down")), FakePrompts())  # type: ignore[arg-type]
    assert await checker.is_answerable(question="q", passages=["p"]) is None


async def test_checker_empty_passages_is_not_answerable() -> None:
    checker = LlmAnswerabilityChecker(FakeChat({"answerable": True}), FakePrompts())  # type: ignore[arg-type]
    assert await checker.is_answerable(question="q", passages=[]) is False


def make_settings(**overrides: object) -> Settings:
    base: dict[str, object] = {
        "_env_file": None,
        "app_env": "test",
        "log_level": "WARNING",
        "log_format": "console",
        "prompt_seed_path": str(SEED_PROMPTS),
        "rag_offline": True,
        "rag_answerability_enabled": True,
        "rag_answerability_threshold": 0.65,
    }
    base.update(overrides)
    return Settings(**base)


class FakeRetriever:
    def __init__(self, score: float, band: ConfidenceBand) -> None:
        self._score = score
        self._band = band

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
                    title="售后政策",
                    section="退款时效",
                    content="余额 1 个工作日到账。",
                    score=self._score,
                )
            ],
            band=self._band,
        )


class StubChecker:
    def __init__(self, answer: bool | None) -> None:
        self._answer = answer
        self.calls = 0

    async def is_answerable(self, *, question: str, passages: list[str]) -> bool | None:
        self.calls += 1
        return self._answer


def build_runtime(settings: Settings, retriever: Any, checker: Any) -> AgentRuntime:
    from app.session.store import SessionStore

    return AgentRuntime(
        settings=settings,
        chat=FakeChat({"answerable": True}),  # type: ignore[arg-type]
        embedding=HashEmbedding(dim=8),
        prompts=FakePrompts(),  # type: ignore[arg-type]
        retriever=retriever,
        vector_index=None,
        checkpointer=InMemorySaver(),
        session_store=SessionStore(None),  # type: ignore[arg-type]
        clients=None,
        answerability=checker,
    )


async def test_low_score_unanswerable_becomes_fallback() -> None:
    checker = StubChecker(False)
    runtime = build_runtime(
        make_settings(), FakeRetriever(0.60, ConfidenceBand.ANSWER_WITH_CAVEAT), checker
    )
    state = initial_state(trace_id="tr", session_id="s", user_id="u")
    state["user_query"] = "海外仓维修怎么预约"
    state["rewritten_query"] = "海外仓维修怎么预约"
    update = await retrieve_node(runtime, state)
    assert checker.calls == 1
    assert update["band"] == "fallback"
    assert "answerability:no" in update["degraded"]


async def test_low_score_answerable_keeps_band() -> None:
    checker = StubChecker(True)
    runtime = build_runtime(
        make_settings(), FakeRetriever(0.60, ConfidenceBand.ANSWER_WITH_CAVEAT), checker
    )
    state = initial_state(trace_id="tr", session_id="s", user_id="u")
    state["user_query"] = "退款多久到账"
    state["rewritten_query"] = "退款多久到账"
    update = await retrieve_node(runtime, state)
    assert update["band"] == "answer_with_caveat"


async def test_high_score_skips_checker() -> None:
    checker = StubChecker(False)
    runtime = build_runtime(make_settings(), FakeRetriever(0.80, ConfidenceBand.ANSWER), checker)
    state = initial_state(trace_id="tr", session_id="s", user_id="u")
    state["user_query"] = "退款多久到账"
    update = await retrieve_node(runtime, state)
    assert checker.calls == 0
    assert update["band"] == "answer"


async def test_checker_disabled_keeps_band() -> None:
    checker = StubChecker(False)
    runtime = build_runtime(
        make_settings(rag_answerability_enabled=False),
        FakeRetriever(0.60, ConfidenceBand.ANSWER_WITH_CAVEAT),
        checker,
    )
    state = initial_state(trace_id="tr", session_id="s", user_id="u")
    state["user_query"] = "海外仓维修怎么预约"
    update = await retrieve_node(runtime, state)
    assert checker.calls == 0
    assert update["band"] == "answer_with_caveat"
