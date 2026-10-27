"""用真实图跑评测用例（Spec §13.3）。

一条用例 = 一次独立的图执行：
- ``thread_id`` 带 uuid，避免与在线会话/更早用例的 checkpoint 串味；
- 不经过 SSE（``get_stream_writer`` 无 writer 时是 no-op），所以断言直接读最终 state；
- 图异常不算"用例失败原因不明"：归因到 ``graph`` 并在报告里可见。
"""

from __future__ import annotations

import time
from typing import Any
from uuid import uuid4

from app.agent.state import initial_state
from app.api.errors import AppError
from app.common.logging import get_logger
from app.eval.assertions import check_expectations
from app.eval.cases import EvalCase
from app.eval.executor import CaseResult

logger = get_logger("app.eval.graph")


class GraphCaseExecutor:
    def __init__(self, graph: Any, *, user_id: str = "eval_user") -> None:
        self._graph = graph
        self._user_id = user_id

    async def run(self, case: EvalCase) -> CaseResult:
        query = str(case.input.get("query", ""))
        history = case.input.get("history") or []
        session_id = f"eval-{case.case_key}-{uuid4().hex[:8]}"
        state = initial_state(
            trace_id=f"tr_eval_{case.case_key}",
            session_id=session_id,
            user_id=self._user_id,
            user_query=query,
            history=history if isinstance(history, list) else [],
        )
        config = {"configurable": {"thread_id": session_id}}
        started = time.perf_counter()
        try:
            await self._graph.ainvoke(state, config)
            snapshot = await self._graph.aget_state(config)
            values: dict[str, Any] = dict(snapshot.values or {})
        except AppError as exc:
            return CaseResult(
                case_key=case.case_key,
                category=str(case.category),
                passed=False,
                failures=[f"图执行失败：{exc.code} {exc.message}"],
                attribution="graph",
                latency_ms=int((time.perf_counter() - started) * 1000),
            )
        except Exception as exc:
            logger.exception("eval.case.failed", extra={"case_key": case.case_key})
            return CaseResult(
                case_key=case.case_key,
                category=str(case.category),
                passed=False,
                failures=[f"图异常：{type(exc).__name__}"],
                attribution="graph",
                latency_ms=int((time.perf_counter() - started) * 1000),
            )

        latency_ms = int((time.perf_counter() - started) * 1000)
        outcome = check_expectations(expected=case.expectation, values=values, user_query=query)
        citations = values.get("citations") or []
        return CaseResult(
            case_key=case.case_key,
            category=str(case.category),
            passed=outcome.passed,
            failures=outcome.failures,
            attribution=outcome.attribution,
            latency_ms=latency_ms,
            intent=str(values.get("intent", "")),
            band=str(values.get("band", "")),
            citations=len(citations) if isinstance(citations, list) else 0,
            answer=str(values.get("answer_stream", "")),
        )
