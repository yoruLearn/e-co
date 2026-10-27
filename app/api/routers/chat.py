"""对话入口（Spec §9.2 ``POST /chat/stream``，SSE）。

M1 行为：LangGraph 状态图 + 流式输出。

SSE 事件（与 Spec 表格一致）：

| event | 触发 |
| --- | --- |
| ``status`` | 节点阶段变化（router 分类完成等） |
| ``delta`` | 用户可见正文增量（已做出口脱敏，按句子切分） |
| ``citation`` | 引用块（模型输出 ``[[n]]`` 时实时下发） |
| ``clarify`` | 低置信/缺参澄清 |
| ``confirm`` | 写操作双确认（M2） |
| ``error`` | 任何失败（统一错误码） |
| ``done`` | 结束（含 trace_id 与 token 用量） |

每帧带 ``id: <seq>``，为 M1-07 的 ``Last-Event-ID`` 续传预留（缓冲与回放见下一迭代）。
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse

from app.agent.runtime import AgentRuntime
from app.agent.state import initial_state
from app.api.deps import PrincipalDep
from app.api.errors import AppError, ErrorCode
from app.api.responses import ERROR_RESPONSES
from app.api.schemas import ChatRequest
from app.api.sse import SSE_HEADERS, SseEvent, format_event
from app.common.trace import get_trace_id

logger = logging.getLogger("app.api.chat")

router = APIRouter(prefix="/chat", tags=["chat"], responses=ERROR_RESPONSES)


def _frame(seq: int, event: SseEvent | str, payload: dict[str, Any]) -> bytes:
    return (f"id: {seq}\n" + format_event(event, payload)).encode()


@router.post(
    "/stream",
    summary="对话（SSE 流式，LangGraph）",
    response_class=StreamingResponse,
    responses={
        200: {
            "description": (
                "SSE 事件流：delta / tool / clarify / confirm / citation / status / error / done"
            ),
            "content": {"text/event-stream": {"schema": {"type": "string"}}},
        }
    },
)
async def chat_stream(
    payload: ChatRequest,
    principal: PrincipalDep,
    request: Request,
) -> StreamingResponse:
    trace_id = get_trace_id() or ""
    runtime: AgentRuntime = request.app.state.agent
    logger.info(
        "chat.stream.request",
        extra={
            "session_id": payload.session_id,
            "user_id": principal.user_id,
            "message_len": len(payload.message),
            "channel": payload.context.channel,
        },
    )

    session = await runtime.session_store.load(payload.session_id)
    state = initial_state(
        trace_id=trace_id,
        session_id=payload.session_id,
        user_id=principal.user_id,
        user_query=payload.message,
        history=session.history(),
    )
    config = {"configurable": {"thread_id": payload.session_id}}

    async def event_stream() -> AsyncIterator[bytes]:
        seq = 0
        answer = ""
        try:
            async for chunk in runtime.graph.astream(state, config, stream_mode="custom"):
                if not isinstance(chunk, dict):
                    continue
                event = str(chunk.get("event", "status"))
                data = chunk.get("payload") or {}
                seq += 1
                yield _frame(seq, event, data)

            snapshot = await runtime.graph.aget_state(config)
            values: dict[str, Any] = dict(snapshot.values or {})
            answer = str(values.get("answer_stream", ""))
            usage_tokens = int(values.get("usage_tokens", 0) or 0)
            seq += 1
            yield _frame(
                seq,
                SseEvent.STATUS,
                {
                    "stage": "done",
                    "intent": str(values.get("intent", "")),
                    "band": values.get("band", ""),
                    "degraded": values.get("degraded", []),
                    "index_version": values.get("index_version", ""),
                },
            )
            seq += 1
            yield _frame(
                seq,
                SseEvent.DONE,
                {"trace_id": trace_id, "usage": {"tokens": usage_tokens}},
            )
            if answer:
                await runtime.session_store.append(
                    payload.session_id,
                    user_message=payload.message,
                    assistant_message=answer,
                )
        except AppError as exc:
            logger.warning("chat.stream.app_error", extra={"code": str(exc.code)})
            seq += 1
            yield _frame(seq, SseEvent.ERROR, {"code": str(exc.code), "message": exc.message})
            seq += 1
            yield _frame(seq, SseEvent.DONE, {"trace_id": trace_id, "usage": {"tokens": 0}})
        except Exception:
            logger.exception("chat.stream.failed")
            seq += 1
            yield _frame(
                seq,
                SseEvent.ERROR,
                {"code": str(ErrorCode.INTERNAL), "message": "服务暂时不可用，请稍后重试"},
            )
            seq += 1
            yield _frame(seq, SseEvent.DONE, {"trace_id": trace_id, "usage": {"tokens": 0}})

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers=SSE_HEADERS,
    )
