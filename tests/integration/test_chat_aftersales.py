"""办事链路图测试：双确认两轮 + 受理结果（Fake 服务，不依赖 MySQL）。"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from decimal import Decimal
from typing import Any

from langgraph.checkpoint.memory import InMemorySaver

from app.agent.graph import build_graph
from app.agent.runtime import AgentRuntime
from app.agent.state import initial_state
from app.common.config import REPO_ROOT, Settings
from app.llm.embeddings import HashEmbedding
from app.llm.types import ChatDelta, ChatMessage, ChatResult, Usage
from app.prompt.registry import PromptRegistry, SeedPromptSource
from app.services.aftersales import ApplyOutcome
from app.session.store import SessionStore

SEED_PROMPTS = REPO_ROOT / "seeds" / "prompts.yaml"


class FakeChat:
    name = "fake"

    def __init__(self, intent: str) -> None:
        self._intent = intent

    async def complete(self, messages: list[ChatMessage], **kwargs: Any) -> ChatResult:
        if kwargs.get("json_mode"):
            return ChatResult(
                content=json.dumps({"intent": self._intent, "confidence": 0.9}),
                model="fake",
            )
        return ChatResult(content="改写后的问题", model="fake")

    async def stream(self, messages: list[ChatMessage], **kwargs: Any) -> AsyncIterator[ChatDelta]:
        yield ChatDelta(content="好的。")
        yield ChatDelta(finish_reason="stop", usage=Usage(total_tokens=5))

    async def aclose(self) -> None:
        return None


class FakeOrderTools:
    async def get_order(self, order_id: str, *, user_id: str, role: str = "buyer") -> Any:
        from types import SimpleNamespace

        return SimpleNamespace(ok=True, data={"order_id": order_id, "amount": "199.00"})


class FakeConfirm:
    def __init__(self) -> None:
        self.issued: list[dict[str, Any]] = []

    async def issue(
        self, *, user_id: str, action: str, order_id: str, amount: Any, trace_id: str
    ) -> str:
        self.issued.append({"action": action, "order_id": order_id, "amount": str(amount)})
        return "ct_test_token"


class FakeWriteTools:
    def __init__(self) -> None:
        self.created: list[dict[str, Any]] = []

    async def create(self, **kwargs: Any) -> ApplyOutcome:
        self.created.append(kwargs)
        return ApplyOutcome(
            biz_id="AS20261028TEST",
            status="PENDING_APPROVAL",
            approval_required=True,
            task_id="ap_1",
        )


class FakeAftersalesService:
    async def get(self, biz_id: str, *, user_id: str, role: str = "buyer") -> dict[str, Any]:
        return {
            "biz_id": biz_id,
            "action": "refund",
            "status": "APPROVED",
            "version": 2,
            "order_id": "O20261027001",
            "amount": 199.0,
            "created_at": "2026-10-28T00:00:00",
            "timeline": [],
        }


def make_settings() -> Settings:
    return Settings(
        _env_file=None,
        app_env="test",
        log_level="WARNING",
        log_format="console",
        rag_offline=True,
        prompt_seed_path=str(SEED_PROMPTS),
        checkpointer_backend="memory",
    )


def build_runtime(
    settings: Settings, intent: str
) -> tuple[AgentRuntime, FakeConfirm, FakeWriteTools]:
    confirm = FakeConfirm()
    write_tools = FakeWriteTools()
    runtime = AgentRuntime(
        settings=settings,
        chat=FakeChat(intent),  # type: ignore[arg-type]
        embedding=HashEmbedding(dim=8),
        prompts=PromptRegistry([SeedPromptSource(SEED_PROMPTS)]),
        retriever=None,  # type: ignore[arg-type]
        vector_index=None,
        checkpointer=InMemorySaver(),
        session_store=SessionStore(None),  # type: ignore[arg-type]
        clients=None,
        order_tools=FakeOrderTools(),  # type: ignore[arg-type]
        confirm=confirm,  # type: ignore[arg-type]
        aftersales=FakeAftersalesService(),  # type: ignore[arg-type]
        aftersales_tools=write_tools,  # type: ignore[arg-type]
    )
    runtime.graph = build_graph(runtime)
    return runtime, confirm, write_tools


async def test_after_sales_two_turn_confirm_and_apply() -> None:
    runtime, confirm, write_tools = build_runtime(make_settings(), "after_sales")
    config = {"configurable": {"thread_id": "as-1"}}

    first = initial_state(
        trace_id="tr_as1", session_id="as-1", user_id="u1", user_query="我要退 O20261027001"
    )
    await runtime.graph.ainvoke(first, config)
    snapshot = await runtime.graph.aget_state(config)
    pending = snapshot.values
    assert pending["confirm_token"] == "ct_test_token"
    assert pending["pending_action"]["order_id"] == "O20261027001"
    assert confirm.issued and confirm.issued[0]["action"] == "return"

    second = initial_state(
        trace_id="tr_as2",
        session_id="as-1",
        user_id="u1",
        user_query="确认",
        confirm_token="ct_test_token",
        pending_action=pending["pending_action"],
    )
    await runtime.graph.ainvoke(second, config)
    result = await runtime.graph.aget_state(config)
    assert result.values["biz_id"] == "AS20261028TEST"
    assert result.values["status"] == "PENDING_APPROVAL"
    assert write_tools.created[0]["confirm_token"] == "ct_test_token"
    assert write_tools.created[0]["amount"] == Decimal("199.00")


async def test_query_status_reads_biz_id() -> None:
    runtime, _confirm, _write = build_runtime(make_settings(), "query")
    config = {"configurable": {"thread_id": "q-1"}}
    state = initial_state(
        trace_id="tr_q", session_id="q-1", user_id="u1", user_query="AS20261028TEST 到哪了"
    )
    await runtime.graph.ainvoke(state, config)
    values = (await runtime.graph.aget_state(config)).values
    assert values["biz_id"] == "AS20261028TEST"
    assert values["status"] == "APPROVED"


async def test_intake_asks_for_order_id_when_missing() -> None:
    runtime, confirm, _write = build_runtime(make_settings(), "after_sales")
    config = {"configurable": {"thread_id": "as-2"}}
    state = initial_state(trace_id="tr_as2", session_id="as-2", user_id="u1", user_query="我要退货")
    await runtime.graph.ainvoke(state, config)
    values = (await runtime.graph.aget_state(config)).values
    assert "订单号" in values["answer_stream"]
    assert values["confirm_token"] is None
    assert confirm.issued == []
