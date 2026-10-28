"""对话入口（Spec §9.2 ``POST /chat/stream``，SSE）。

M1 行为：LangGraph 状态图 + 流式输出 + 断线续传。

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

续传：每帧带会话级递增 ``id``；客户端重连带 ``Last-Event-ID``，服务端只回放该 id 之后的帧
（天然"不重放已发送事件"），无帧可回放时返回错误提示让用户重新提问。
"""

from __future__ import annotations

import logging
import time
from collections.abc import AsyncIterator
from typing import Annotated, Any

from fastapi import APIRouter, Header, Request
from fastapi.responses import StreamingResponse

from app.agent.runtime import AgentRuntime
from app.agent.state import initial_state
from app.api.deps import PrincipalDep
from app.api.errors import AppError, ErrorCode
from app.api.responses import ERROR_RESPONSES
from app.api.schemas import ChatRequest
from app.api.sse import SSE_HEADERS, SseEvent
from app.api.sse_buffer import SseBuffer, SseFrame
from app.common.metrics import CHAT_FIRST_TOKEN, CHAT_LATENCY
from app.common.trace import get_trace_id

logger = logging.getLogger("app.api.chat")

router = APIRouter(prefix="/chat", tags=["chat"], responses=ERROR_RESPONSES)

EXPIRED_REPLAY_TEXT = "会话事件已过期或不存在，请重新提问。"


def settings_session_lock_ttl(request: Request) -> int:
    return int(getattr(request.app.state.settings, "session_lock_ttl_sec", 60))


def _parse_last_event_id(raw: str | None) -> int | None:
    if raw is None or not raw.strip():
        return None
    try:
        value = int(raw.strip())
    except ValueError as exc:
        raise AppError(
            ErrorCode.PARAM_INVALID,
            "Last-Event-ID 必须是整数",
            detail={"value": raw[:32]},
        ) from exc
    if value < 0:
        raise AppError(ErrorCode.PARAM_INVALID, "Last-Event-ID 不能为负")
    return value


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
    last_event_id: Annotated[str | None, Header(alias="Last-Event-ID")] = None,
) -> StreamingResponse:
    trace_id = get_trace_id() or ""
    runtime: AgentRuntime = request.app.state.agent
    buffer: SseBuffer = runtime.sse_buffer
    session_id = payload.session_id
    replay_after = _parse_last_event_id(last_event_id)

    logger.info(
        "chat.stream.request",
        extra={
            "session_id": session_id,
            "user_id": principal.user_id,
            "message_len": len(payload.message),
            "channel": payload.context.channel,
            "resume_after": replay_after,
        },
    )

    if replay_after is not None:
        frames = await buffer.since(session_id, replay_after)

        async def replay() -> AsyncIterator[bytes]:
            if not frames:
                expired = SseFrame(
                    id=await buffer.next_id(session_id),
                    event=str(SseEvent.ERROR),
                    payload={
                        "code": str(ErrorCode.PARAM_INVALID),
                        "message": EXPIRED_REPLAY_TEXT,
                        "trace_id": trace_id,
                    },
                )
                await buffer.append(session_id, expired)
                yield expired.to_bytes()
                done = SseFrame(
                    id=await buffer.next_id(session_id),
                    event=str(SseEvent.DONE),
                    payload={"trace_id": trace_id, "usage": {"tokens": 0}},
                )
                await buffer.append(session_id, done)
                yield done.to_bytes()
                return
            for frame in frames:
                yield frame.to_bytes()

        return StreamingResponse(replay(), media_type="text/event-stream", headers=SSE_HEADERS)

    limiter = request.app.state.ratelimit
    allowed, retry_after = await limiter.allow(principal.user_id)
    if not allowed:
        raise AppError(
            ErrorCode.RATE_LIMITED,
            "请求过于频繁，请稍后再试",
            detail={"retry_after_seconds": retry_after},
        )
    if not await limiter.acquire_session(session_id):
        raise AppError(
            ErrorCode.RATE_LIMITED,
            "该会话正在处理上一条消息，请等待完成后再发送",
            detail={"retry_after_seconds": settings_session_lock_ttl(request)},
        )

    session = await runtime.session_store.load(session_id)
    state = initial_state(
        trace_id=trace_id,
        session_id=session_id,
        user_id=principal.user_id,
        user_query=payload.message,
        history=session.history(),
    )
    config = {"configurable": {"thread_id": session_id}}

    async def emit(event: SseEvent | str, body: dict[str, Any]) -> AsyncIterator[bytes]:
        frame = SseFrame(
            id=await buffer.next_id(session_id),
            event=str(event),
            payload=body,
        )
        await buffer.append(session_id, frame)
        yield frame.to_bytes()

    async def event_stream() -> AsyncIterator[bytes]:
        answer = ""
        started = time.perf_counter()
        first_token_at: float | None = None
        usage_for_mode = 0
        try:
            async for chunk in runtime.graph.astream(state, config, stream_mode="custom"):
                if not isinstance(chunk, dict):
                    continue
                if first_token_at is None:
                    first_token_at = time.perf_counter() - started
                async for data in emit(
                    str(chunk.get("event", "status")), chunk.get("payload") or {}
                ):
                    yield data

            snapshot = await runtime.graph.aget_state(config)
            values: dict[str, Any] = dict(snapshot.values or {})
            answer = str(values.get("answer_stream", ""))
            usage_tokens = int(values.get("usage_tokens", 0) or 0)
            usage_for_mode = usage_tokens
            async for data in emit(
                SseEvent.STATUS,
                {
                    "stage": "done",
                    "intent": str(values.get("intent", "")),
                    "band": values.get("band", ""),
                    "degraded": values.get("degraded", []),
                    "index_version": values.get("index_version", ""),
                },
            ):
                yield data
            async for data in emit(
                SseEvent.DONE, {"trace_id": trace_id, "usage": {"tokens": usage_tokens}}
            ):
                yield data
            if answer:
                await runtime.session_store.append(
                    session_id,
                    user_message=payload.message,
                    assistant_message=answer,
                )
        except AppError as exc:
            logger.warning("chat.stream.app_error", extra={"code": str(exc.code)})
            async for data in emit(SseEvent.ERROR, {"code": str(exc.code), "message": exc.message}):
                yield data
            async for data in emit(SseEvent.DONE, {"trace_id": trace_id, "usage": {"tokens": 0}}):
                yield data
        except Exception:
            logger.exception("chat.stream.failed")
            async for data in emit(
                SseEvent.ERROR,
                {"code": str(ErrorCode.INTERNAL), "message": "服务暂时不可用，请稍后重试"},
            ):
                yield data
            async for data in emit(SseEvent.DONE, {"trace_id": trace_id, "usage": {"tokens": 0}}):
                yield data
        finally:
            await limiter.release_session(session_id)
            CHAT_LATENCY.observe(time.perf_counter() - started)
            if first_token_at is not None:
                CHAT_FIRST_TOKEN.labels(
                    mode="generated" if usage_for_mode > 0 else "fallback"
                ).observe(first_token_at)

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers=SSE_HEADERS,
    )
