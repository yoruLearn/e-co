"""Prompt Registry（Spec §6.3）。

- 生产：读 ``prompt_registry`` 表，仅用 ``status='published'`` 的版本，``key@version`` 可回滚；
- 离线/CI：回落到 ``seeds/prompts.yaml``（bundled 基线），保证没有数据库也能跑图；
- 进程内缓存（``PROMPT_CACHE_TTL_SEC``），发布/回滚后调用 ``invalidate()`` 立即生效。

表结构在 M0 已建（`prompt_registry`），M1 只实现读路径；写路径（发布/回滚 + 审计）属 M3。
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from string import Template
from typing import Any, Protocol

import yaml
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.api.errors import AppError, ErrorCode
from app.common.config import Settings
from app.common.logging import get_logger, log_event, log_warning
from app.db.models import PromptRegistry as PromptRegistryRow

logger = get_logger("app.prompt.registry")


class PromptKey(StrEnum):
    ROUTER = "router"
    REWRITE = "rewrite"
    ANSWER_RAG = "answer_rag"
    CLARIFY = "clarify"
    REJECT = "reject"
    RERANK = "rerank"


@dataclass(frozen=True, slots=True)
class PromptTemplate:
    key: str
    version: int
    content: str
    status: str = "published"
    source: str = "seed"
    updated_by: str = "system"


class PromptSource(Protocol):
    name: str

    async def load(self, key: str, version: int | None) -> PromptTemplate | None: ...


class SeedPromptSource:
    """bundled YAML（``seeds/prompts.yaml``），离线与 CI 的基线来源。"""

    name = "seed"

    def __init__(self, path: Path) -> None:
        self._path = path
        self._cache: dict[str, PromptTemplate] | None = None

    def _load_all(self) -> dict[str, PromptTemplate]:
        if self._cache is not None:
            return self._cache
        if not self._path.exists():
            self._cache = {}
            return self._cache
        raw: dict[str, Any] = yaml.safe_load(self._path.read_text(encoding="utf-8")) or {}
        prompts = raw.get("prompts") or {}
        parsed: dict[str, PromptTemplate] = {}
        for key, item in prompts.items():
            if not isinstance(item, dict) or "content" not in item:
                continue
            parsed[str(key)] = PromptTemplate(
                key=str(key),
                version=int(item.get("version", 1)),
                content=str(item["content"]).strip(),
                status=str(item.get("status", "published")),
                source=self.name,
                updated_by=str(item.get("updated_by", "system")),
            )
        self._cache = parsed
        return parsed

    async def load(self, key: str, version: int | None) -> PromptTemplate | None:
        template = self._load_all().get(key)
        if template is None:
            return None
        if version is not None and template.version != version:
            return None
        return template

    def keys(self) -> list[str]:
        return sorted(self._load_all())


class DbPromptSource:
    """``prompt_registry`` 表；只认 published（draft 不得上线）。"""

    name = "db"

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = session_factory

    async def load(self, key: str, version: int | None) -> PromptTemplate | None:
        async with self._sessions() as session:
            statement = select(PromptRegistryRow).where(PromptRegistryRow.prompt_key == key)
            if version is None:
                statement = statement.where(PromptRegistryRow.status == "published").order_by(
                    PromptRegistryRow.version.desc()
                )
            else:
                statement = statement.where(PromptRegistryRow.version == version)
            row = await session.scalar(statement.limit(1))
        if row is None or row.status != "published":
            return None
        return PromptTemplate(
            key=row.prompt_key,
            version=row.version,
            content=row.content,
            status=row.status,
            source=self.name,
            updated_by=row.updated_by,
        )


@dataclass(slots=True)
class _CacheEntry:
    template: PromptTemplate
    expires_at: float


class PromptRegistry:
    """按 ``key@version`` 读取提示词；多来源按顺序回落。"""

    def __init__(
        self,
        sources: list[PromptSource],
        *,
        cache_ttl_sec: int = 60,
    ) -> None:
        self._sources = sources
        self._ttl = max(0, cache_ttl_sec)
        self._cache: dict[tuple[str, int | None], _CacheEntry] = {}

    @classmethod
    def from_settings(
        cls,
        settings: Settings,
        *,
        session_factory: async_sessionmaker[AsyncSession] | None = None,
        seed_path: Path | None = None,
    ) -> PromptRegistry:
        sources: list[PromptSource] = []
        if session_factory is not None:
            sources.append(DbPromptSource(session_factory))
        sources.append(SeedPromptSource(seed_path or Path(settings.prompt_seed_path)))
        return cls(sources, cache_ttl_sec=settings.prompt_cache_ttl_sec)

    async def get(self, key: str | PromptKey, version: int | None = None) -> PromptTemplate:
        cache_key = (str(key), version)
        now = time.monotonic()
        cached = self._cache.get(cache_key)
        if cached is not None and cached.expires_at > now:
            return cached.template

        for source in self._sources:
            try:
                template = await source.load(str(key), version)
            except Exception as exc:
                log_warning(
                    logger,
                    "prompt.source.failed",
                    source=source.name,
                    prompt_key=str(key),
                    reason=type(exc).__name__,
                )
                continue
            if template is not None:
                self._cache[cache_key] = _CacheEntry(template=template, expires_at=now + self._ttl)
                log_event(
                    logger,
                    "prompt.loaded",
                    prompt_key=template.key,
                    version=template.version,
                    source=template.source,
                )
                return template

        raise AppError(
            ErrorCode.INTERNAL,
            f"Prompt 不存在：{key}@{version or 'latest'}",
            detail={"prompt_key": str(key), "version": version},
        )

    async def render(
        self, key: str | PromptKey, *, version: int | None = None, **values: Any
    ) -> str:
        """取模板并做 ``string.Template`` 替换。

        为什么不用 ``str.format``：提示词里大量出现 JSON 花括号（``{"intent": ...}``），
        ``format`` 会被误判为占位符；``$question`` 风格没有这个歧义。
        缺变量抛 ``KeyError``——编程错误应快速失败，而不是给模型一段残缺提示。
        """
        template = await self.get(key, version)
        return Template(template.content).substitute(**values)

    def invalidate(self, key: str | PromptKey | None = None) -> None:
        if key is None:
            self._cache.clear()
            return
        for cache_key in [item for item in self._cache if item[0] == str(key)]:
            self._cache.pop(cache_key, None)

    @property
    def source_names(self) -> list[str]:
        return [source.name for source in self._sources]
