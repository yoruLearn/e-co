"""``POST /eval/run`` 集成测试：真接口 + Fake 图，不依赖 MySQL/Chroma/模型。

断言重点：指标返回、门禁状态、用例目录可配、权限（ops/admin vs buyer）。
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from fastapi.testclient import TestClient
from langgraph.checkpoint.memory import InMemorySaver

from app.common.config import Settings
from app.main import create_app


class FakeGraph:
    """按 query 返回预置 state；同时记录调用次数。"""

    def __init__(self, responses: dict[str, dict[str, Any]]) -> None:
        self._responses = responses
        self._by_thread: dict[str, dict[str, Any]] = {}
        self.calls = 0

    async def ainvoke(self, state: dict[str, Any], config: dict[str, Any]) -> None:
        self.calls += 1
        thread = config["configurable"]["thread_id"]
        query = state.get("user_query", "")
        self._by_thread[thread] = self._responses.get(query, {"intent": "qa", "answer_stream": ""})

    async def aget_state(self, config: dict[str, Any]) -> Any:
        thread = config["configurable"]["thread_id"]
        return SimpleNamespace(values=self._by_thread.get(thread, {}))


class FakeRuntime:
    """只实现 lifespan/接口用到的三样：graph、checkpointer、close()。"""

    def __init__(self, graph: FakeGraph) -> None:
        self.graph = graph
        self.checkpointer = InMemorySaver()

    async def close(self) -> None:
        return None


def make_settings(cases_dir: Path) -> Settings:
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
        eval_cases_dir=str(cases_dir),
        ready_timeout_ms=100,
    )


def write_cases(tmp_path: Path) -> Path:
    cases = [
        {
            "case_key": "eval-ok",
            "category": "typical",
            "input": {"query": "退款多久到账", "history": []},
            "expectation": {"intent": "qa", "must_cite": True},
        },
        {
            "case_key": "eval-bad-intent",
            "category": "typical",
            "input": {"query": "我的退款到哪了", "history": []},
            "expectation": {"intent": "query"},
        },
    ]
    (tmp_path / "cases.json").write_text(json.dumps(cases, ensure_ascii=False), encoding="utf-8")
    return tmp_path


def build_client(
    tmp_path: Path, responses: dict[str, dict[str, Any]]
) -> tuple[TestClient, FakeGraph]:
    settings = make_settings(write_cases(tmp_path))
    graph = FakeGraph(responses)
    app = create_app(settings)
    app.state.agent = FakeRuntime(graph)
    return TestClient(app), graph


def test_eval_run_returns_metrics_and_gate(tmp_path: Path, ops_auth: dict[str, str]) -> None:
    responses = {
        "退款多久到账": {
            "intent": "qa",
            "band": "answer",
            "answer_stream": "余额 1 个工作日到账[[1]]。",
            "citations": [
                {"doc_id": "p", "title": "政策", "section": "退款时效", "snippet": "1 个工作日"}
            ],
            "usage_tokens": 30,
        },
        "我的退款到哪了": {
            "intent": "qa",  # 期望 query → 失败
            "band": "answer",
            "answer_stream": "已受理[[1]]。",
            "citations": [{"doc_id": "p", "title": "政策", "section": "退款", "snippet": "受理"}],
        },
    }
    client, graph = build_client(tmp_path, responses)
    with client:
        response = client.post("/api/v1/eval/run", json={"suite": "unit"}, headers=ops_auth)
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["total"] == 2
    assert data["pass_cnt"] == 1
    assert data["completion_rate"] == 0.5
    assert data["gate_passed"] is False
    assert data["status"] == "gate_failed"
    assert data["attribution"] == {"router": 1}
    assert graph.calls == 2


def test_eval_run_requires_operator_role(tmp_path: Path, buyer_auth: dict[str, str]) -> None:
    client, _graph = build_client(tmp_path, {})
    with client:
        response = client.post("/api/v1/eval/run", json={"suite": "unit"}, headers=buyer_auth)
    assert response.status_code == 403
    assert response.json()["code"] == "AS-4030"


def test_eval_run_requires_auth(tmp_path: Path) -> None:
    client, _graph = build_client(tmp_path, {})
    with client:
        response = client.post("/api/v1/eval/run", json={"suite": "unit"})
    assert response.status_code == 401


def test_eval_run_with_no_cases_returns_business_error(
    tmp_path: Path, ops_auth: dict[str, str]
) -> None:
    settings = make_settings(tmp_path / "empty")
    app = create_app(settings)
    app.state.agent = FakeRuntime(FakeGraph({}))
    with TestClient(app) as client:
        response = client.post(
            "/api/v1/eval/run",
            json={"suite": "unit", "categories": ["no_answer"]},
            headers=ops_auth,
        )
    assert response.status_code == 422
    assert response.json()["code"] == "AS-4002"
