"""``/chat/stream`` 端到端（SSE）：真实图 + Fake 模型/检索器，不依赖外部服务。

验证的是**协议与图行为**：事件顺序、引用下发、脱敏、无引用拦截、会话落记忆。
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

import pytest
from fastapi.testclient import TestClient
from langgraph.checkpoint.memory import InMemorySaver

from app.agent.graph import build_graph
from app.agent.runtime import AgentRuntime
from app.agent.state import Intent
from app.common.config import REPO_ROOT, Settings
from app.llm.embeddings import HashEmbedding
from app.llm.types import ChatDelta, ChatMessage, ChatResult, Usage
from app.main import create_app
from app.prompt.registry import PromptRegistry, SeedPromptSource
from app.rag.pipeline import RetrievalHit, RetrievalResult
from app.rag.retriever import ConfidenceBand
from app.services.ratelimit import RateLimiter
from app.session.store import SessionState

SEED_PROMPTS = REPO_ROOT / "seeds" / "prompts.yaml"


class FakeChat:
    name = "fake"

    def __init__(self, answer: str = "退款 1-3 个工作日到账[[1]]。", *, intent: str = "qa") -> None:
        self._answer = answer
        self._intent = intent
        self.complete_calls: list[dict[str, Any]] = []
        self.stream_calls = 0

    async def complete(self, messages: list[ChatMessage], **kwargs: Any) -> ChatResult:
        self.complete_calls.append({"messages": messages, **kwargs})
        if not kwargs.get("json_mode"):
            # rewrite / summary 等非结构化调用
            return ChatResult(content="改写后的问题", model="fake", usage=Usage(total_tokens=8))
        payload = {"intent": self._intent, "confidence": 0.92}
        return ChatResult(
            content=json.dumps(payload, ensure_ascii=False),
            model="fake",
            usage=Usage(prompt_tokens=10, completion_tokens=5, total_tokens=15),
        )

    async def stream(self, messages: list[ChatMessage], **kwargs: Any) -> AsyncIterator[ChatDelta]:
        for start in range(0, len(self._answer), 6):
            yield ChatDelta(content=self._answer[start : start + 6])
        yield ChatDelta(finish_reason="stop", usage=Usage(total_tokens=42))

    async def aclose(self) -> None:
        return None


class FakeRetriever:
    def __init__(
        self,
        hits: list[RetrievalHit] | None = None,
        *,
        band: ConfidenceBand = ConfidenceBand.ANSWER,
    ) -> None:
        self._hits = hits if hits is not None else [_default_hit()]
        self._band = band
        self.queries: list[str] = []

    async def retrieve(
        self, query: str, *, index_version: str, collection_id: str | None, top_k: int | None = None
    ) -> RetrievalResult:
        self.queries.append(query)
        return RetrievalResult(
            query=query,
            index_version=index_version,
            hits=self._hits,
            band=self._band,
        )


class FakeSessionStore:
    def __init__(self) -> None:
        self.appended: list[tuple[str, str, str]] = []
        self.state = SessionState()

    async def load(self, session_id: str) -> SessionState:
        return self.state

    async def append(
        self, session_id: str, *, user_message: str, assistant_message: str
    ) -> SessionState:
        self.appended.append((session_id, user_message, assistant_message))
        return self.state


def _default_hit() -> RetrievalHit:
    return RetrievalHit(
        chunk_id="售后政策总则#0",
        doc_id="售后政策总则",
        title="售后政策总则",
        section="退款时效",
        content="退货退款在商家签收后原路退回，余额 1 个工作日内到账。",
        score=0.91,
    )


def make_settings(**overrides: object) -> Settings:
    base: dict[str, object] = {
        "_env_file": None,
        "app_env": "test",
        "log_level": "WARNING",
        "log_format": "console",
        "shutdown_drain_seconds": 0,
        "database_url": "mysql+aiomysql://u:p@127.0.0.1:1/x",
        "redis_url": "redis://127.0.0.1:1/0",
        "chroma_url": "http://127.0.0.1:1",
        "checkpointer_backend": "memory",
        "prompt_seed_path": str(SEED_PROMPTS),
        "rag_index_version": "v1",
    }
    base.update(overrides)
    return Settings(**base)


def build_fake_runtime(
    settings: Settings,
    *,
    chat: FakeChat | None = None,
    retriever: FakeRetriever | None = None,
    session_store: FakeSessionStore | None = None,
) -> AgentRuntime:
    runtime = AgentRuntime(
        settings=settings,
        chat=chat or FakeChat(),  # type: ignore[arg-type]
        embedding=HashEmbedding(dim=8),
        prompts=PromptRegistry([SeedPromptSource(SEED_PROMPTS)]),
        retriever=retriever or FakeRetriever(),  # type: ignore[arg-type]
        vector_index=None,
        checkpointer=InMemorySaver(),
        session_store=session_store or FakeSessionStore(),  # type: ignore[arg-type]
        clients=None,
    )
    runtime.graph = build_graph(runtime)
    return runtime


def parse_frames(body: str) -> list[dict[str, Any]]:
    frames: list[dict[str, Any]] = []
    for raw in body.split("\n\n"):
        if not raw.strip():
            continue
        frame: dict[str, Any] = {"lines": raw.splitlines()}
        for line in raw.splitlines():
            if line.startswith("id: "):
                frame["id"] = int(line[4:])
            elif line.startswith("event: "):
                frame["event"] = line[7:]
            elif line.startswith("data: "):
                frame["data"] = json.loads(line[6:])
        frames.append(frame)
    return frames


def stream_chat(client: TestClient, auth: dict[str, str], message: str, session: str = "sess-1"):
    with client.stream(
        "POST",
        "/api/v1/chat/stream",
        json={"session_id": session, "message": message},
        headers=auth,
    ) as response:
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        body = b"".join(response.iter_bytes()).decode("utf-8")
    return parse_frames(body)


def test_chat_stream_happy_path_emits_citation_and_redacts(
    settings: Settings, buyer_auth: dict[str, str]
) -> None:
    runtime = build_fake_runtime(
        settings,
        chat=FakeChat("退款 1-3 个工作日到账[[1]]。客服电话 13812345678。"),
    )
    app = create_app(settings)
    app.state.agent = runtime
    with TestClient(app) as client:
        frames = stream_chat(client, buyer_auth, "退款多久到账")

    events = [frame["event"] for frame in frames]
    assert events[0] == "status"
    assert "delta" in events
    assert "citation" in events
    assert events[-1] == "done"
    assert events[-2] == "status"

    citation = next(frame for frame in frames if frame["event"] == "citation")
    assert citation["data"]["id"] == 1
    assert citation["data"]["doc_id"] == "售后政策总则"

    text = "".join(frame["data"]["text"] for frame in frames if frame["event"] == "delta")
    assert "13812345678" not in text
    assert "138****5678" in text
    assert "[[1]]" in text

    done = frames[-1]["data"]
    assert done["trace_id"]
    assert done["usage"]["tokens"] == 42

    assert runtime.session_store.appended  # type: ignore[attr-defined]
    session_id, user_message, assistant_message = runtime.session_store.appended[0]  # type: ignore[attr-defined]
    assert session_id == "sess-1"
    assert user_message == "退款多久到账"
    assert assistant_message


def test_chat_stream_blocks_answer_without_citation(
    settings: Settings, buyer_auth: dict[str, str]
) -> None:
    runtime = build_fake_runtime(settings, chat=FakeChat("退款一般很快到账。"))
    app = create_app(settings)
    app.state.agent = runtime
    with TestClient(app) as client:
        frames = stream_chat(client, buyer_auth, "退款多久到账")

    events = [frame["event"] for frame in frames]
    assert "clarify" in events, "无引用必须降级为澄清，不得直出"
    clarify = next(frame for frame in frames if frame["event"] == "clarify")
    assert "人工" in clarify["data"]["question"]


def test_chat_stream_chitchat_is_rejected(settings: Settings, buyer_auth: dict[str, str]) -> None:
    runtime = build_fake_runtime(settings, chat=FakeChat(intent="chitchat"))
    app = create_app(settings)
    app.state.agent = runtime
    with TestClient(app) as client:
        frames = stream_chat(client, buyer_auth, "你好呀")
    texts = "".join(frame["data"].get("text", "") for frame in frames if frame["event"] == "delta")
    assert "售后" in texts


def test_chat_stream_pending_capability_for_query_intent(
    settings: Settings, buyer_auth: dict[str, str]
) -> None:
    runtime = build_fake_runtime(settings, chat=FakeChat(intent="query"))
    app = create_app(settings)
    app.state.agent = runtime
    with TestClient(app) as client:
        frames = stream_chat(client, buyer_auth, "我的退款到哪了")
    texts = "".join(frame["data"].get("text", "") for frame in frames if frame["event"] == "delta")
    assert "M2" in texts


def test_chat_stream_low_confidence_clarifies(
    settings: Settings, buyer_auth: dict[str, str]
) -> None:
    runtime = build_fake_runtime(
        settings, retriever=FakeRetriever([], band=ConfidenceBand.FALLBACK)
    )
    app = create_app(settings)
    app.state.agent = runtime
    with TestClient(app) as client:
        frames = stream_chat(client, buyer_auth, "跨境订单被扣了怎么办")
    events = [frame["event"] for frame in frames]
    assert "clarify" in events
    assert "delta" in events


def test_chat_stream_requires_auth(client: TestClient) -> None:
    response = client.post("/api/v1/chat/stream", json={"session_id": "s", "message": "你好"})
    assert response.status_code == 401
    assert response.json()["code"] == "AS-4010"


@pytest.mark.parametrize("message", ["退款多久到账"])
def test_frame_ids_are_sequential(
    settings: Settings, buyer_auth: dict[str, str], message: str
) -> None:
    runtime = build_fake_runtime(settings)
    app = create_app(settings)
    app.state.agent = runtime
    with TestClient(app) as client:
        frames = stream_chat(client, buyer_auth, message)
    ids = [frame["id"] for frame in frames]
    assert ids == list(range(1, len(ids) + 1))


def test_intent_enum_roundtrip() -> None:
    assert Intent("qa") is Intent.QA


def test_resume_replays_only_frames_after_last_event_id(
    settings: Settings, buyer_auth: dict[str, str]
) -> None:
    runtime = build_fake_runtime(settings)
    app = create_app(settings)
    app.state.agent = runtime
    with TestClient(app) as client:
        first = stream_chat(client, buyer_auth, "退款多久到账", session="resume-1")
        assert len(first) >= 4
        calls_before = runtime.chat.stream_calls  # type: ignore[attr-defined]

        with client.stream(
            "POST",
            "/api/v1/chat/stream",
            json={"session_id": "resume-1", "message": "继续"},
            headers={**buyer_auth, "Last-Event-ID": str(first[0]["id"])},
        ) as response:
            body = b"".join(response.iter_bytes()).decode("utf-8")
    replayed = parse_frames(body)

    assert [frame["id"] for frame in replayed] == [frame["id"] for frame in first[1:]]
    assert runtime.chat.stream_calls == calls_before, "续传不应重新生成答案"  # type: ignore[attr-defined]


def test_resume_with_unknown_session_returns_error(
    settings: Settings, buyer_auth: dict[str, str]
) -> None:
    runtime = build_fake_runtime(settings)
    app = create_app(settings)
    app.state.agent = runtime
    with (
        TestClient(app) as client,
        client.stream(
            "POST",
            "/api/v1/chat/stream",
            json={"session_id": "never-seen", "message": "继续"},
            headers={**buyer_auth, "Last-Event-ID": "99"},
        ) as response,
    ):
        body = b"".join(response.iter_bytes()).decode("utf-8")
    frames = parse_frames(body)
    assert frames[0]["event"] == "error"
    assert frames[0]["data"]["code"] == "AS-4001"
    assert frames[-1]["event"] == "done"


def test_resume_with_invalid_header_returns_400(
    settings: Settings, buyer_auth: dict[str, str]
) -> None:
    app = create_app(settings)
    app.state.agent = build_fake_runtime(settings)
    with TestClient(app) as client:
        response = client.post(
            "/api/v1/chat/stream",
            json={"session_id": "s", "message": "hi"},
            headers={**buyer_auth, "Last-Event-ID": "abc"},
        )
    assert response.status_code == 400
    assert response.json()["code"] == "AS-4001"


def test_multi_turn_history_triggers_rewrite(
    settings: Settings, buyer_auth: dict[str, str]
) -> None:
    runtime = build_fake_runtime(settings)
    runtime.session_store.state = SessionState(  # type: ignore[attr-defined]
        turns=[
            {"role": "user", "content": "我的耳机能退吗"},
            {"role": "assistant", "content": "请提供订单号或确认最近一笔订单"},
        ]
    )
    app = create_app(settings)
    app.state.agent = runtime
    with TestClient(app) as client:
        frames = stream_chat(client, buyer_auth, "那这一单呢", session="multi-1")

    retriever: FakeRetriever = runtime.retriever  # type: ignore[assignment]
    assert retriever.queries and retriever.queries[0] == "改写后的问题"
    chat: FakeChat = runtime.chat  # type: ignore[assignment]
    assert len(chat.complete_calls) >= 2, "多轮应同时调用 router 与 rewrite"
    assert frames[-1]["event"] == "done"


class FakeRateRedis:
    def __init__(self) -> None:
        self.sorted_sets: dict[str, dict[str, float]] = {}
        self.kv: dict[str, str] = {}

    async def zremrangebyscore(self, key: str, minimum: float, maximum: float) -> int:
        items = self.sorted_sets.get(key, {})
        removed = [n for n, s in items.items() if minimum <= s <= maximum]
        for name in removed:
            items.pop(name, None)
        return len(removed)

    async def zadd(self, key: str, mapping: dict[str, float]) -> int:
        self.sorted_sets.setdefault(key, {}).update(mapping)
        return len(mapping)

    async def zcard(self, key: str) -> int:
        return len(self.sorted_sets.get(key, {}))

    async def expire(self, key: str, seconds: int) -> bool:
        return True

    async def set(self, key: str, value: str, *, nx: bool = False, ex: int | None = None):
        if nx and key in self.kv:
            return None
        self.kv[key] = value
        return True

    async def delete(self, *keys: str) -> int:
        return sum(1 for key in keys if self.kv.pop(key, None) is not None)


def test_chat_rate_limited_returns_429(settings: Settings, buyer_auth: dict[str, str]) -> None:
    app = create_app(settings)
    app.state.agent = build_fake_runtime(settings)
    app.state.ratelimit = RateLimiter(FakeRateRedis(), limit_per_min=1)
    with TestClient(app) as client:
        with client.stream(
            "POST",
            "/api/v1/chat/stream",
            json={"session_id": "rl-1", "message": "退款多久到账"},
            headers=buyer_auth,
        ) as response:
            assert response.status_code == 200
            b"".join(response.iter_bytes())
        second = client.post(
            "/api/v1/chat/stream",
            json={"session_id": "rl-2", "message": "退款多久到账"},
            headers=buyer_auth,
        )
    assert second.status_code == 429
    assert second.json()["code"] == "AS-4290"


def test_chat_session_busy_returns_429(settings: Settings, buyer_auth: dict[str, str]) -> None:
    redis = FakeRateRedis()
    redis.kv["lock:chat:busy-1"] = "1"
    app = create_app(settings)
    app.state.agent = build_fake_runtime(settings)
    app.state.ratelimit = RateLimiter(redis, limit_per_min=20)
    with TestClient(app) as client:
        response = client.post(
            "/api/v1/chat/stream",
            json={"session_id": "busy-1", "message": "退款多久到账"},
            headers=buyer_auth,
        )
    assert response.status_code == 429
    assert "上一条消息" in response.json()["message"]


def test_chat_latency_metric_recorded(settings: Settings, buyer_auth: dict[str, str]) -> None:
    from prometheus_client import REGISTRY

    before = REGISTRY.get_sample_value("chat_latency_seconds_count") or 0.0
    app = create_app(settings)
    app.state.agent = build_fake_runtime(settings)
    with TestClient(app) as client:
        stream_chat(client, buyer_auth, "退款多久到账", session="metrics-1")
    after = REGISTRY.get_sample_value("chat_latency_seconds_count") or 0.0
    assert after > before
