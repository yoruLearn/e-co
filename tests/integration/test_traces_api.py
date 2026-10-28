"""TraceRecorder 与 ``GET /traces/{id}`` 测试（Fake session factory，不依赖 MySQL）。"""

from __future__ import annotations

from typing import Any

from fastapi.testclient import TestClient
from langgraph.checkpoint.memory import InMemorySaver

from app.common.config import Settings
from app.main import create_app
from app.services.trace_store import NullTraceRecorder, TraceRecorder


class FakeSession:
    def __init__(self, store: list[Any]) -> None:
        self._store = store

    def add(self, obj: Any) -> None:
        self._store.append(obj)

    async def commit(self) -> None:
        return None

    async def __aenter__(self) -> FakeSession:
        return self

    async def __aexit__(self, *args: Any) -> None:
        return None


class FakeFactory:
    def __init__(self) -> None:
        self.rows: list[Any] = []

    def __call__(self) -> FakeSession:
        return FakeSession(self.rows)


class FakeRuntime:
    def __init__(self, trace: Any) -> None:
        from types import SimpleNamespace

        self.graph = SimpleNamespace()
        self.checkpointer = InMemorySaver()
        self.trace = trace

    async def close(self) -> None:
        return None


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
        ready_timeout_ms=100,
    )


async def test_recorder_persists_span() -> None:
    factory = FakeFactory()
    recorder = TraceRecorder(factory)  # type: ignore[arg-type]
    await recorder.record(trace_id="tr_1", node="router", start_ms=1000, duration_ms=12)
    assert len(factory.rows) == 1
    row = factory.rows[0]
    assert row.trace_id == "tr_1"
    assert row.node == "router"
    assert row.duration_ms == 12
    assert row.status == "ok"


async def test_recorder_skips_empty_trace_id() -> None:
    factory = FakeFactory()
    recorder = TraceRecorder(factory)  # type: ignore[arg-type]
    await recorder.record(trace_id="", node="router", duration_ms=1)
    assert factory.rows == []


async def test_recorder_swallows_db_failure() -> None:
    class BrokenFactory:
        def __call__(self) -> Any:
            raise RuntimeError("db down")

    recorder = TraceRecorder(BrokenFactory())  # type: ignore[arg-type]
    await recorder.record(trace_id="tr_1", node="router", duration_ms=1)  # 不抛


async def test_null_recorder_is_noop() -> None:
    recorder = NullTraceRecorder()
    await recorder.record(trace_id="tr_1", node="router", duration_ms=1)
    assert await recorder.fetch("tr_1") == []


def test_trace_endpoint_returns_spans(agent_auth: dict[str, str]) -> None:
    class FakeRecorder:
        async def fetch(self, trace_id: str) -> list[dict[str, Any]]:
            if trace_id != "tr_abc":
                return []
            return [
                {
                    "span": "router",
                    "start_ms": 1000.0,
                    "duration_ms": 12.0,
                    "status": "ok",
                    "tokens": 0,
                    "meta": {},
                },
                {
                    "span": "answer",
                    "start_ms": 1050.0,
                    "duration_ms": 800.0,
                    "status": "ok",
                    "tokens": 120,
                    "meta": {"session_id": "s1"},
                },
            ]

    app = create_app(make_settings())
    app.state.agent = FakeRuntime(FakeRecorder())
    with TestClient(app) as client:
        response = client.get("/api/v1/traces/tr_abc", headers=agent_auth)
        assert response.status_code == 200, response.text
        spans = response.json()["data"]["spans"]
        assert [span["span"] for span in spans] == ["router", "answer"]
        assert spans[1]["tokens"] == 120

        missing = client.get("/api/v1/traces/tr_missing", headers=agent_auth)
        assert missing.status_code == 404
        assert missing.json()["code"] == "AS-4040"


def test_trace_endpoint_requires_agent_role(buyer_auth: dict[str, str]) -> None:
    app = create_app(make_settings())
    app.state.agent = FakeRuntime(NullTraceRecorder())
    with TestClient(app) as client:
        response = client.get("/api/v1/traces/tr_abc", headers=buyer_auth)
    assert response.status_code == 403
