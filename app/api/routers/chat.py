"""对话入口（Spec §9.2 ``POST /chat/stream``，SSE）。

M0 只保证**协议骨架**：正确的 ``text/event-stream``、反代不缓冲、事件格式可解析。
M1 在此接入 LangGraph 流式输出（``delta``/``tool``/``citation``/``clarify``/``confirm``）。

M0 行为：先推 ``error``(AS-5002) 再推 ``done``，HTTP 状态仍为 200 —— 客户端按事件分派，
不会把"里程碑未到"当成网关错误。
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator

from fastapi import APIRouter
from fastapi.responses import StreamingResponse

from app.api.deps import PrincipalDep
from app.api.errors import ErrorCode
from app.api.responses import ERROR_RESPONSES
from app.api.schemas import ChatRequest
from app.api.sse import SSE_HEADERS, SseEvent, format_event
from app.common.trace import get_trace_id

logger = logging.getLogger("app.api.chat")

router = APIRouter(prefix="/chat", tags=["chat"], responses=ERROR_RESPONSES)


@router.post(
    "/stream",
    summary="对话（SSE 流式）",
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
async def chat_stream(payload: ChatRequest, principal: PrincipalDep) -> StreamingResponse:
    trace_id = get_trace_id()
    logger.info(
        "chat.stream.request",
        extra={
            "session_id": payload.session_id,
            "user_id": principal.user_id,
            "message_len": len(payload.message),
            "channel": payload.context.channel,
        },
    )

    async def event_stream() -> AsyncIterator[bytes]:
        yield format_event(
            SseEvent.ERROR,
            {
                "code": str(ErrorCode.NOT_IMPLEMENTED),
                "message": "对话链路（意图路由 / RAG / 工具）将于 M1 交付",
                "trace_id": trace_id,
            },
        ).encode()
        yield format_event(
            SseEvent.DONE,
            {"trace_id": trace_id, "usage": {"tokens": 0, "llm_calls": 0, "cache_hits": 0}},
        ).encode()

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers=SSE_HEADERS,
    )
