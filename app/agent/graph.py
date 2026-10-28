"""LangGraph 图装配（Spec §6.1 / §6.2）。

编排约束（与 M0 锁定的常量一致）：

1. 条件边只读 state，不发网络请求（checkpoint 重放安全）；
2. 每节点输入前裁剪上下文（``MAX_TOOL_RESULTS`` / ``MAX_CITATIONS``）；
3. 节点级调用上限（router/rewrite/answer 各 3 次）由图结构保证——不设回边，天然无循环；
4. checkpointer 由运行时注入，``thread_id = session_id`` 支撑多轮与（M2）审批挂起。

M1 的图只覆盖问答链路；``query``/``after_sales`` 走 ``pending_capability``（M2 接工具）。
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any, Final

from langgraph.graph import END, START, StateGraph

from app.agent.nodes import core
from app.agent.state import AfterSalesState
from app.common.logging import get_logger, log_event

if TYPE_CHECKING:
    from app.agent.runtime import AgentRuntime

logger = get_logger("app.agent.graph")

NODE_NAMES: Final[tuple[str, ...]] = (
    "router",
    "rewrite",
    "retrieve",
    "answer",
    "tools_ro",
    "confirm",
    "hitl_confirm",
    "state_machine",
    "approval",
    "clarify",
    "reject",
    "pending_capability",
    "sse_out",
)

NODE_TIMEOUTS_MS: Final[dict[str, int]] = {
    "router": 2000,
    "rewrite": 3000,
    "retrieve": 1200,
    "answer": 8000,
    "tools_ro": 800,
    "state_machine": 1000,
}

MAX_NODE_CALLS: Final[dict[str, int]] = {"router": 3, "rewrite": 3, "answer": 3}
MAX_TOOL_RESULTS: Final[int] = 5
MAX_CITATIONS: Final[int] = 3


def build_graph(runtime: AgentRuntime) -> Any:
    """编译状态图；节点用闭包持有 runtime（依赖可替换，便于测试注入 Fake）。"""
    graph = StateGraph(AfterSalesState)

    async def timed(
        node_name: str,
        state: AfterSalesState,
        handler: Callable[[], Awaitable[dict[str, Any]]],
    ) -> dict[str, Any]:
        """记录节点耗时；trace 写失败不影响节点结果。"""
        started_ms = int(time.time() * 1000)
        started = time.perf_counter()
        status = "ok"
        try:
            return await handler()
        except Exception:
            status = "error"
            raise
        finally:
            await runtime.trace.record(
                trace_id=str(state.get("trace_id") or ""),
                node=node_name,
                start_ms=started_ms,
                duration_ms=int((time.perf_counter() - started) * 1000),
                status=status,
                meta={"session_id": str(state.get("session_id") or "")},
            )

    async def router(state: AfterSalesState) -> dict[str, Any]:
        return await timed("router", state, lambda: core.router_node(runtime, state))

    async def rewrite(state: AfterSalesState) -> dict[str, Any]:
        return await timed("rewrite", state, lambda: core.rewrite_node(runtime, state))

    async def retrieve(state: AfterSalesState) -> dict[str, Any]:
        return await timed("retrieve", state, lambda: core.retrieve_node(runtime, state))

    async def answer(state: AfterSalesState) -> dict[str, Any]:
        return await timed("answer", state, lambda: core.answer_node(runtime, state))

    async def clarify(state: AfterSalesState) -> dict[str, Any]:
        return await timed("clarify", state, lambda: core.clarify_node(runtime, state))

    async def reject(state: AfterSalesState) -> dict[str, Any]:
        return await timed("reject", state, lambda: core.reject_node(runtime, state))

    async def pending_capability(state: AfterSalesState) -> dict[str, Any]:
        return await timed("pending_capability", state, lambda: core.pending_node(runtime, state))

    async def sse_out(state: AfterSalesState) -> dict[str, Any]:
        return await timed("sse_out", state, lambda: core.sse_out_node(runtime, state))

    graph.add_node("router", router)
    graph.add_node("rewrite", rewrite)
    graph.add_node("retrieve", retrieve)
    graph.add_node("answer", answer)
    graph.add_node("clarify", clarify)
    graph.add_node("reject", reject)
    graph.add_node("pending_capability", pending_capability)
    graph.add_node("sse_out", sse_out)

    graph.add_edge(START, "router")
    graph.add_conditional_edges(
        "router",
        core.route_after_router,
        {
            "rewrite": "rewrite",
            "pending_capability": "pending_capability",
            "reject": "reject",
        },
    )
    graph.add_edge("rewrite", "retrieve")
    graph.add_conditional_edges(
        "retrieve",
        core.route_after_retrieve,
        {"answer": "answer", "clarify": "clarify"},
    )
    graph.add_edge("answer", "sse_out")
    graph.add_edge("clarify", "sse_out")
    graph.add_edge("reject", "sse_out")
    graph.add_edge("pending_capability", "sse_out")
    graph.add_edge("sse_out", END)

    compiled = graph.compile(checkpointer=runtime.checkpointer)
    log_event(
        logger,
        "graph.compiled",
        node_count=len(NODE_NAMES),
        checkpointer=type(runtime.checkpointer).__name__,
    )
    return compiled
