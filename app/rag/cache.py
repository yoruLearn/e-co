"""检索缓存键规范（Spec §7.3 / §11.1）。

缓存键必须在**一个地方**生成：索引版本切换时要按前缀删除（§11.1 ``idx:active``），
键格式一旦分叉就会漏删，出现"旧索引结果返给用户"的线上事故。
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Final

from app.common.config import Settings

_WHITESPACE: Final[re.Pattern[str]] = re.compile(r"\s+")


def normalize_query(query: str) -> str:
    """去首尾空白 + 折叠内部空白（大小写保留，避免语义漂移）。"""
    return _WHITESPACE.sub(" ", query.strip())


def query_hash(query: str, *, length: int = 16) -> str:
    digest = hashlib.sha256(normalize_query(query).encode("utf-8")).hexdigest()
    return digest[:length]


def rag_cache_key(index_version: str, query: str) -> str:
    """``rag:{index_ver}:{hash(query)}`` —— 绑定索引版本，版本切换后自然失效。"""
    return f"rag:{index_version}:{query_hash(query)}"


def local_cache_key(index_version: str, query: str) -> str:
    """进程内（60s）缓存键 ``lc:{hash(query+ver)}``。"""
    return f"lc:{query_hash(f'{query}|{index_version}')}"


def answer_cache_key(index_version: str, query: str) -> str:
    """``ans:{index_ver}:{hash(query)}`` —— 答案级缓存，随索引版本失效。"""
    return f"ans:{index_version}:{query_hash(query)}"


def faq_cache_key(question: str) -> str:
    return f"faq:{query_hash(question)}"


def fact_cache_key(sku_id: str) -> str:
    return f"fact:sku:{sku_id}"


def session_key(session_id: str) -> str:
    return f"sess:{session_id}"


def checkpoint_key(session_id: str) -> str:
    return f"ckpt:{session_id}"


def idem_cache_key(idem_key: str) -> str:
    return f"idem:{idem_key}"


def lock_key(biz_id: str) -> str:
    return f"lock:as:{biz_id}"


def confirm_token_key(token: str) -> str:
    return f"confirm:{token}"


def rate_limit_key(user_id: str, route: str) -> str:
    return f"rl:{user_id}:{route}"


def breaker_error_key(window: str) -> str:
    return f"cb:write:error:{window}"


ACTIVE_INDEX_KEY: Final[str] = "idx:active"
INDEX_VERSION_PREFIX: Final[str] = "rag:"


@dataclass(frozen=True, slots=True)
class TtlPolicy:
    """TTL 集中配置（Spec §7.3），避免各模块各写魔法数字。"""

    rag_seconds: int
    faq_seconds: int
    fact_seconds: int
    session_seconds: int
    idem_seconds: int

    @classmethod
    def from_settings(cls, settings: Settings) -> TtlPolicy:
        return cls(
            rag_seconds=settings.rag_cache_ttl_sec,
            faq_seconds=settings.faq_cache_ttl_sec,
            fact_seconds=settings.fact_cache_ttl_sec,
            session_seconds=settings.session_ttl_hours * 3600,
            idem_seconds=settings.idempotency_ttl_sec,
        )
