"""LangGraph checkpointer（Spec §6.2 第 5 条 / FR-AS-06）。

M0 只定义契约；M1 起使用 ``RedisSaver``（会话级 checkpoint，TTL 24h），
支撑 ``approval`` 节点跨进程恢复。
"""

from __future__ import annotations

from dataclasses import dataclass

from app.common.config import Settings


@dataclass(frozen=True, slots=True)
class CheckpointPolicy:
    ttl_hours: int
    namespace: str = "ckpt"

    @classmethod
    def from_settings(cls, settings: Settings) -> CheckpointPolicy:
        return cls(ttl_hours=settings.session_ttl_hours)


def build_checkpointer(settings: Settings) -> object:
    """构造可持久化 checkpointer（RedisSaver）。

    TODO(M1): 依赖 ``langgraph-checkpoint-redis``；M0 不引入以免骨架依赖过重。
    """
    raise NotImplementedError("checkpointer 将在 M1 实现（Spec §6.2）")
