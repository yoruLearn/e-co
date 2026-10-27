"""LangGraph 节点实现（Spec §6.1 / §6.2）。

节点契约：

- 输入只读 ``AfterSalesState``，输出**部分更新**字典；
- 所有网络 I/O 只在节点内部（条件边不发起请求，保证 checkpoint 重放安全）；
- 用户可见文本通过 ``get_stream_writer()`` 发 ``delta``/``citation``/``clarify`` 事件，
  同时汇总进 ``answer_stream`` 供落 checkpoint 与后续评测；
- 任何节点失败都不抛穿整图：降级为可读话术 + ``degraded`` 记录。
"""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from langgraph.config import get_stream_writer

from app.agent.citations import (
    build_context_blocks,
    citation_for_marker,
    has_citation,
    referenced_ids,
    to_citations,
)
from app.agent.state import AfterSalesState, Intent
from app.common.metrics import ANSWER_WITHOUT_CITATION
from app.common.trace import get_trace_id
from app.llm.types import ChatMessage, LLMError
from app.prompt.guard import StreamingRedactor, redact_output, with_system_guard, wrap_user_input

if TYPE_CHECKING:
    from app.agent.runtime import AgentRuntime

ROUTER_TIMEOUT_S = 2.0
REWRITE_TIMEOUT_S = 3.0
RETRIEVE_TIMEOUT_S = 1.5
ANSWER_TIMEOUT_S = 20.0

NO_ANSWER_TEXT = (
    "抱歉，我暂时没有找到足够准确的售后政策依据，不想给您不准确的信息。"
    "请补充商品名称或订单号，我帮您转人工核实。"
)
CHITCHAT_TEXT = (
    "我只负责本平台的售后问题：查询订单/物流/退款进度、解释售后政策、发起退换货。您想了解哪一项？"
)
PENDING_TEXT = (
    "订单查询与售后办理能力还在开发中（M2 交付）；现在可以先为您解答售后政策问题。"
    "如果事情比较急，请提供订单号并联系人工客服处理。"
)

_AFTER_SALES_HINTS = ("我要退", "申请售后", "帮我退", "退货申请", "换货", "退掉", "要退")
_QUERY_HINTS = ("我的订单", "这单", "到哪了", "进度", "物流", "快递", "发货", "签收", "单号")
_QA_HINTS = (
    "政策",
    "规则",
    "怎么",
    "如何",
    "多久",
    "能不能",
    "可以吗",
    "条件",
    "标准",
    "保修",
    "运费",
)
_GREETING_HINTS = ("你好", "您好", "在吗", "hi", "hello", "谢谢", "再见")
_PUNCT = re.compile(r"[\s。！？?!.，,；;]")


def _writer() -> Callable[[dict[str, Any]], None]:
    try:
        return get_stream_writer()
    except Exception:
        return lambda _payload: None


def history_text(history: list[dict[str, str]], *, max_turns: int = 6) -> str:
    turns = history[-max_turns:] if history else []
    if not turns:
        return "（无历史）"
    return "\n".join(f"{item.get('role', 'user')}: {item.get('content', '')}" for item in turns)


def classify_by_rules(text: str) -> tuple[Intent, float]:
    """规则兜底（router 模型超时/输出非法时使用）：宁可判 qa 走检索，也不误办业务。"""
    cleaned = _PUNCT.sub("", text or "")
    if not cleaned:
        return Intent.CHITCHAT, 0.3
    if any(hint in cleaned for hint in _AFTER_SALES_HINTS) or (
        "退款" in cleaned and "申请" in cleaned
    ):
        return Intent.AFTER_SALES, 0.55
    if any(hint in cleaned for hint in _QUERY_HINTS) or (
        "退款" in cleaned and "到账" in cleaned and "多久" not in cleaned
    ):
        return Intent.QUERY, 0.55
    if any(hint in cleaned for hint in _QA_HINTS) or "退款" in cleaned:
        return Intent.QA, 0.6
    if any(hint in cleaned for hint in _GREETING_HINTS) or len(cleaned) <= 4:
        return Intent.CHITCHAT, 0.5
    return Intent.QA, 0.4


def with_timeout(coro: Any, seconds: float) -> Any:
    return asyncio.wait_for(coro, timeout=seconds)


async def router_node(runtime: AgentRuntime, state: AfterSalesState) -> dict[str, Any]:
    question = state.get("user_query", "")
    degraded = list(state.get("degraded", []))
    writer = _writer()
    intent: Intent | None = None
    confidence = 0.0
    try:
        rendered = await runtime.prompts.render(
            "router",
            history=history_text(state.get("history", [])),
            question=wrap_user_input(question),
        )
        result = await with_timeout(
            runtime.chat.complete(
                [
                    ChatMessage(role="system", content=with_system_guard("你是意图分类器。")),
                    ChatMessage(role="user", content=rendered),
                ],
                temperature=0.0,
                max_tokens=200,
                json_mode=True,
                tag="router",
            ),
            ROUTER_TIMEOUT_S,
        )
        payload = json.loads(result.content)
        intent = Intent(str(payload.get("intent", "qa")))
        confidence = float(payload.get("confidence", 0.6))
    except (TimeoutError, LLMError, json.JSONDecodeError, ValueError, KeyError) as exc:
        degraded.append(f"router:{type(exc).__name__}")
        intent, confidence = classify_by_rules(question)
    writer({"event": "status", "payload": {"stage": "router", "intent": str(intent)}})
    return {
        "intent": str(intent),
        "router_confidence": confidence,
        "degraded": degraded,
        "trace_id": state.get("trace_id") or get_trace_id() or "",
    }


async def rewrite_node(runtime: AgentRuntime, state: AfterSalesState) -> dict[str, Any]:
    question = state.get("user_query", "")
    history = state.get("history", [])
    degraded = list(state.get("degraded", []))
    rewritten = question
    if history:
        try:
            rendered = await runtime.prompts.render(
                "rewrite",
                history=history_text(history),
                question=wrap_user_input(question),
            )
            result = await with_timeout(
                runtime.chat.complete(
                    [
                        ChatMessage(role="system", content=with_system_guard("你是查询改写器。")),
                        ChatMessage(role="user", content=rendered),
                    ],
                    temperature=0.0,
                    max_tokens=160,
                    tag="rewrite",
                ),
                REWRITE_TIMEOUT_S,
            )
            candidate = result.content.strip().strip('"').strip("“”")
            if candidate:
                rewritten = candidate
        except (TimeoutError, LLMError) as exc:
            degraded.append(f"rewrite:{type(exc).__name__}")
    return {"rewritten_query": rewritten, "degraded": degraded}


async def retrieve_node(runtime: AgentRuntime, state: AfterSalesState) -> dict[str, Any]:
    query = state.get("rewritten_query") or state.get("user_query", "")
    degraded = list(state.get("degraded", []))
    version = await runtime.active_index_version()
    collection_id = await runtime.collection_id(version)
    try:
        result = await with_timeout(
            runtime.retriever.retrieve(query, index_version=version, collection_id=collection_id),
            max(RETRIEVE_TIMEOUT_S, runtime.settings.ready_timeout_ms / 1000 + 1.0),
        )
    except Exception as exc:
        degraded.append(f"retrieve:{type(exc).__name__}")
        return {
            "band": "fallback",
            "citations": [],
            "context_blocks": [],
            "degraded": degraded,
            "index_version": version,
        }
    hits = result.hits
    return {
        "citations": to_citations(hits),
        "context_blocks": build_context_blocks(hits),
        "band": str(result.band),
        "degraded": degraded + list(result.degraded),
        "index_version": version,
        "rewritten_query": query,
    }


async def answer_node(runtime: AgentRuntime, state: AfterSalesState) -> dict[str, Any]:
    question = state.get("rewritten_query") or state.get("user_query", "")
    citations = list(state.get("citations", []))
    blocks = list(state.get("context_blocks", []))
    degraded = list(state.get("degraded", []))
    writer = _writer()
    redactor = StreamingRedactor()
    emitted: set[int] = set()
    collected: list[str] = []
    usage_tokens = 0

    def _emit_pending_citations(text: str) -> None:
        for marker_id in referenced_ids(text):
            if marker_id in emitted or marker_id > len(citations):
                continue
            citation = citation_for_marker(citations, marker_id)
            if citation is None:
                continue
            emitted.add(marker_id)
            writer({"event": "citation", "payload": citation.model_dump()})

    try:
        rendered = await runtime.prompts.render(
            "answer_rag",
            context="\n\n".join(blocks),
            question=wrap_user_input(question),
        )
        messages = [
            ChatMessage(
                role="system",
                content=with_system_guard("你是电商售后管家，只根据资料回答。"),
            ),
            ChatMessage(role="user", content=rendered),
        ]
        async with asyncio.timeout(ANSWER_TIMEOUT_S):
            async for delta in runtime.chat.stream(messages, temperature=0.0, tag="answer"):
                if delta.usage is not None:
                    usage_tokens = delta.usage.total_tokens or usage_tokens
                if not delta.content:
                    continue
                collected.append(delta.content)
                flushed = redactor.feed(delta.content)
                if flushed:
                    writer({"event": "delta", "payload": {"text": flushed}})
                    _emit_pending_citations(flushed)
        tail = redactor.flush()
        if tail:
            writer({"event": "delta", "payload": {"text": tail}})
            _emit_pending_citations(tail)
    except (TimeoutError, LLMError) as exc:
        degraded.append(f"answer:{type(exc).__name__}")
        writer(
            {
                "event": "error",
                "payload": {"code": "AS-5033", "message": "回答生成失败，请稍后重试"},
            }
        )
        return {"answer_stream": NO_ANSWER_TEXT, "degraded": degraded, "band": "fallback"}

    answer = "".join(collected)
    if citations and not has_citation(answer):
        # G2：无引用不得直出——用兜底话术替换，并记录指标（应为 0）
        ANSWER_WITHOUT_CITATION.inc()
        degraded.append("answer:no_citation")
        writer({"event": "delta", "payload": {"text": "\n\n" + NO_ANSWER_TEXT}})
        writer(
            {
                "event": "clarify",
                "payload": {"question": NO_ANSWER_TEXT, "missing": ["更准确的资料依据"]},
            }
        )
        answer = NO_ANSWER_TEXT
    return {
        "answer_stream": redact_output(answer).text,
        "degraded": degraded,
        "usage_tokens": usage_tokens,
    }


async def clarify_node(runtime: AgentRuntime, state: AfterSalesState) -> dict[str, Any]:
    writer = _writer()
    text = NO_ANSWER_TEXT
    writer({"event": "delta", "payload": {"text": text}})
    writer({"event": "clarify", "payload": {"question": text, "missing": ["更准确的资料依据"]}})
    return {"answer_stream": text, "band": "fallback"}


async def reject_node(runtime: AgentRuntime, state: AfterSalesState) -> dict[str, Any]:
    writer = _writer()
    writer({"event": "delta", "payload": {"text": CHITCHAT_TEXT}})
    return {"answer_stream": CHITCHAT_TEXT}


async def pending_node(runtime: AgentRuntime, state: AfterSalesState) -> dict[str, Any]:
    writer = _writer()
    writer({"event": "delta", "payload": {"text": PENDING_TEXT}})
    return {"answer_stream": PENDING_TEXT}


async def sse_out_node(runtime: AgentRuntime, state: AfterSalesState) -> dict[str, Any]:
    """终态聚合：不再产生文本，只为图结构保留显式出口（便于后续加事件/落库）。"""
    return {}


def route_after_router(state: AfterSalesState) -> str:
    intent = str(state.get("intent", Intent.QA))
    if intent == Intent.QA:
        return "rewrite"
    if intent in (Intent.QUERY, Intent.AFTER_SALES):
        return "pending_capability"
    return "reject"


def route_after_retrieve(state: AfterSalesState) -> str:
    return "clarify" if state.get("band") == "fallback" else "answer"
