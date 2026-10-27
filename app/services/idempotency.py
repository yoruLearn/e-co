"""幂等键与参数指纹（Spec §9.1 / §11.2 / §8.2）。

M0 只做 key 规则与指纹（纯函数）；Redis 层（``SET idem:{key} NX EX 86400`` + 参数比对 +
分布式锁）在 M2 与状态机一起落地。规则先行可以避免 M1/M2 各自造 key 导致对不上。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Final

from app.api.errors import IdempotencyConflictError
from app.services.state_machine import AfterSalesEvent

IDEM_KEY_MAX_LEN: Final[int] = 80


class IdempotencyDecision(StrEnum):
    NEW = "new"
    REPLAY = "replay"
    CONFLICT = "conflict"


@dataclass(frozen=True, slots=True)
class IdempotencyRecord:
    """首次请求的落库结果引用（Redis value）。"""

    params_hash: str
    response_ref: str


def build_idem_key(biz_id: str, action: str | AfterSalesEvent) -> str:
    """``{bizId}:{action}``；长度受 ``uk_idem VARCHAR(80)`` 约束。"""
    key = f"{biz_id}:{action}"
    if len(key) > IDEM_KEY_MAX_LEN:
        msg = f"幂等键长度 {len(key)} 超过 {IDEM_KEY_MAX_LEN}: {key}"
        raise ValueError(msg)
    return key


def parse_idem_key(key: str) -> tuple[str, str]:
    if key.count(":") < 1:
        msg = f"幂等键格式必须是 {{bizId}}:{{action}}，当前 {key!r}"
        raise ValueError(msg)
    biz_id, action = key.rsplit(":", 1)
    return biz_id, action


def canonical_payload(payload: dict[str, Any]) -> str:
    """稳定序列化：键排序 + 紧凑分隔符，保证同参同指纹。"""
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def params_hash(payload: dict[str, Any]) -> str:
    return hashlib.sha256(canonical_payload(payload).encode("utf-8")).hexdigest()


def decide(
    existing: IdempotencyRecord | None,
    payload: dict[str, Any],
) -> IdempotencyDecision:
    """幂等决策：无记录→NEW；参数一致→REPLAY；参数不一致→CONFLICT(AS-4004)。"""
    if existing is None:
        return IdempotencyDecision.NEW
    if existing.params_hash == params_hash(payload):
        return IdempotencyDecision.REPLAY
    return IdempotencyDecision.CONFLICT


def ensure_idempotent(
    existing: IdempotencyRecord | None,
    payload: dict[str, Any],
    *,
    idem_key: str,
) -> IdempotencyDecision:
    """``decide`` 的抛错版本，供路由直接调用。"""
    decision = decide(existing, payload)
    if decision is IdempotencyDecision.CONFLICT:
        raise IdempotencyConflictError(
            idem_key,
            detail={
                "existing_params_hash": existing.params_hash if existing else None,
                "incoming_params_hash": params_hash(payload),
            },
        )
    return decision
