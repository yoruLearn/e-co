"""LangGraph 图装配（Spec §6.1 / §6.2）。

M0 只锁定**节点清单与编排约束**，避免 M1 时节点命名/顺序各写一套：

1. 条件边只读 state，不发网络请求；
2. 写节点幂等（``(biz_id, action)``）；
3. ``router``/``rewrite``/``answer`` 每图最多 3 次；
4. 每节点输入裁剪：``tool_results`` 最近 5 条、``citations`` 最近 3 条；
5. checkpointer：``RedisSaver``（会话级 24h），审批挂起必须持久化。
"""

from __future__ import annotations

from typing import Final

from app.common.config import Settings

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


def build_graph(settings: Settings) -> object:
    """编译 LangGraph 状态图。

    TODO(M1): 节点实现 + 条件边 + ``RedisSaver`` 装配（Spec §6.1）。
    """
    raise NotImplementedError("Agent 图装配将在 M1/M2 实现（Spec §6）")
