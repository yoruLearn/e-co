"""SSE 事件协议（Spec §9.2）。

格式严格为 ``event: <type>\\ndata: <json>\\n\\n``；客户端按 ``event`` 分派。
M1 在此基础上接入 LLM token 流与工具事件；M0 只保证协议骨架可用。
"""

from __future__ import annotations

import json
from enum import StrEnum
from typing import Any


class SseEvent(StrEnum):
    DELTA = "delta"
    TOOL = "tool"
    CLARIFY = "clarify"
    CONFIRM = "confirm"
    CITATION = "citation"
    STATUS = "status"
    ERROR = "error"
    DONE = "done"


SSE_HEADERS: dict[str, str] = {
    "Cache-Control": "no-cache",
    "Connection": "keep-alive",
    # nginx 反代下必须关闭缓冲，否则 token 会被攒着发（Spec §2.1 接入层）
    "X-Accel-Buffering": "no",
}


def format_event(event: SseEvent | str, data: dict[str, Any]) -> str:
    """序列化为一条 SSE frame。"""
    event_name = str(event)
    payload = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    return f"event: {event_name}\ndata: {payload}\n\n"
