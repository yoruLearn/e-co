"""Agent 状态工厂测试（Spec §5.3）。"""

from __future__ import annotations

from app.agent.graph import MAX_CITATIONS, MAX_NODE_CALLS, MAX_TOOL_RESULTS, NODE_NAMES
from app.agent.state import AfterSalesState, Citation, Intent, ToolResult, initial_state


def test_initial_state_has_all_spec_fields() -> None:
    state = initial_state(trace_id="tr_0123456789abcdef", session_id="s1", user_id="u1")
    expected_keys = {
        "trace_id",
        "session_id",
        "user_id",
        "user_query",
        "history",
        "intent",
        "router_confidence",
        "rewritten_query",
        "slots",
        "missing_slots",
        "tool_results",
        "citations",
        "context_blocks",
        "band",
        "degraded",
        "index_version",
        "biz_id",
        "action",
        "status",
        "version",
        "confirm_required",
        "approval_task_id",
        "answer_stream",
        "cache_hit",
        "user_role",
        "user_confirm_token",
        "confirm_token",
        "pending_action",
        "usage_tokens",
        "error",
    }
    assert set(state) == expected_keys
    assert state["version"] == 0
    assert state["status"] == "DRAFT"
    assert state["confirm_required"] is False
    assert state["trace_id"] == "tr_0123456789abcdef"


def test_intent_enum_matches_spec_four_classes() -> None:
    assert {item.value for item in Intent} == {"qa", "query", "after_sales", "chitchat"}


def test_citation_and_tool_result_models() -> None:
    citation = Citation(
        id=1, doc_id="d1", title="售后政策", section="3.2", snippet="七天无理由", score=0.9
    )
    assert citation.model_dump()["section"] == "3.2"
    result = ToolResult(name="get_order", ok=True, data={"order_id": "O1"}, cost_ms=12)
    assert result.degraded is False
    assert result.error_code is None


def test_graph_contract_constants() -> None:
    assert len(NODE_NAMES) == len(set(NODE_NAMES))
    assert {"router", "retrieve", "answer", "state_machine", "sse_out"} <= set(NODE_NAMES)
    assert MAX_NODE_CALLS == {"router": 3, "rewrite": 3, "answer": 3}
    assert MAX_TOOL_RESULTS == 5
    assert MAX_CITATIONS == 3


def test_state_is_typed_dict_compatible() -> None:
    state: AfterSalesState = {"trace_id": "tr_x", "intent": Intent.QA.value}
    assert state["intent"] == "qa"
