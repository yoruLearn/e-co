"""就绪检查（Spec §9.2：``/readyz`` 校验 MySQL/Redis/MQ/Chroma 连通与迁移版本）。

约定：

- 每个依赖一个 Checker，并发探测，**单检查受统一超时约束**，总耗时 ≈ 最大单项耗时；
- 探测失败只影响 ``/readyz`` 的 503，绝不影响 ``/healthz``（存活）；
- 未启用的组件（如 dev 未起 MQ）标记 ``skipped`` 而不是 ``ok``，不伪装健康；
- 明细里不出现连接串/密码。
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Protocol

import httpx
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from app.common.clients import AppClients
from app.common.config import Settings

CheckStatus = Literal["ok", "fail", "skipped"]

_ALEMBIC_VERSION_SQL = "SELECT version_num FROM alembic_version LIMIT 1"


@dataclass(slots=True)
class CheckResult:
    name: str
    status: CheckStatus
    detail: str = ""
    latency_ms: int = 0

    @property
    def healthy(self) -> bool:
        return self.status != "fail"

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "status": self.status,
            "detail": self.detail,
            "latency_ms": self.latency_ms,
        }


class Checker(Protocol):
    name: str
    enabled: bool

    async def probe(self) -> str:
        """成功返回可读明细；失败抛异常。"""
        ...


class MySqlChecker:
    name = "mysql"
    enabled = True

    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    async def probe(self) -> str:
        async with self._engine.connect() as conn:
            result = await conn.execute(text("SELECT VERSION()"))
            version = result.scalar_one()
        return f"mysql {version}"


class MigrationChecker:
    """校验 ``alembic_version`` 与脚本 head 一致（Spec §17.3 迁移先行）。"""

    name = "migrations"
    enabled = True

    def __init__(self, engine: AsyncEngine, migrations_dir: Path) -> None:
        self._engine = engine
        self._migrations_dir = migrations_dir

    async def probe(self) -> str:
        head = self._script_head()
        async with self._engine.connect() as conn:
            result = await conn.execute(text(_ALEMBIC_VERSION_SQL))
            row = result.first()
        if row is None:
            msg = "alembic_version 表为空，尚未执行 alembic upgrade head"
            raise RuntimeError(msg)
        current = str(row[0])
        if head and current != head:
            msg = f"数据库版本 {current} 落后于脚本 head {head}"
            raise RuntimeError(msg)
        return f"schema at {current}"

    def _script_head(self) -> str | None:
        from alembic.config import Config
        from alembic.script import ScriptDirectory

        if not self._migrations_dir.exists():
            return None
        config = Config()
        config.set_main_option("script_location", str(self._migrations_dir))
        return ScriptDirectory.from_config(config).get_current_head()


class RedisChecker:
    name = "redis"
    enabled = True

    def __init__(self, clients: AppClients) -> None:
        self._redis = clients.redis

    async def probe(self) -> str:
        pong = self._redis.ping()
        if not isinstance(pong, bool):
            pong = await pong
        if not pong:
            msg = "redis ping 未返回 PONG"
            raise RuntimeError(msg)
        return "PONG"


class MQChecker:
    """RocketMQ NameServer TCP 可达性（M0 只做端口探测，协议接入在 M2）。"""

    def __init__(self, settings: Settings) -> None:
        self.name = "mq"
        self.enabled = settings.mq_enabled
        self._host = settings.mq_host
        self._port = settings.mq_port

    async def probe(self) -> str:
        _reader, writer = await asyncio.open_connection(self._host, self._port)
        writer.close()
        with contextlib.suppress(ConnectionError, OSError):
            # 关闭异常不影响探测结论
            await writer.wait_closed()
        return f"nameserver {self._host}:{self._port} reachable"


class ChromaChecker:
    name = "chroma"
    enabled = True

    def __init__(self, settings: Settings, http: httpx.AsyncClient) -> None:
        self._base = settings.chroma_url.rstrip("/")
        self._http = http

    async def probe(self) -> str:
        response = await self._http.get(f"{self._base}/api/v1/heartbeat")
        response.raise_for_status()
        return f"heartbeat {response.status_code}"


@dataclass(slots=True)
class ReadinessReport:
    ready: bool
    checks: list[CheckResult]
    duration_ms: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": "ready" if self.ready else "not_ready",
            "duration_ms": self.duration_ms,
            "checks": [item.as_dict() for item in self.checks],
        }


async def _run_one(checker: Checker, timeout_s: float) -> CheckResult:
    started = time.perf_counter()
    if not checker.enabled:
        return CheckResult(name=checker.name, status="skipped", detail="disabled by config")
    try:
        detail = await asyncio.wait_for(checker.probe(), timeout=timeout_s)
    except TimeoutError:
        elapsed = int((time.perf_counter() - started) * 1000)
        return CheckResult(
            name=checker.name,
            status="fail",
            detail=f"timeout after {timeout_s}s",
            latency_ms=elapsed,
        )
    except Exception as exc:
        elapsed = int((time.perf_counter() - started) * 1000)
        return CheckResult(
            name=checker.name,
            status="fail",
            detail=f"{type(exc).__name__}: {exc}",
            latency_ms=elapsed,
        )
    elapsed = int((time.perf_counter() - started) * 1000)
    return CheckResult(name=checker.name, status="ok", detail=detail, latency_ms=elapsed)


class ReadinessService:
    def __init__(self, checkers: list[Checker], timeout_s: float) -> None:
        self._checkers = checkers
        self._timeout_s = timeout_s

    async def check(self) -> ReadinessReport:
        started = time.perf_counter()
        results = await asyncio.gather(
            *(_run_one(item, self._timeout_s) for item in self._checkers)
        )
        duration_ms = int((time.perf_counter() - started) * 1000)
        return ReadinessReport(
            ready=all(item.healthy for item in results),
            checks=list(results),
            duration_ms=duration_ms,
        )


def build_readiness_service(settings: Settings, clients: AppClients) -> ReadinessService:
    checkers: list[Checker] = [
        MySqlChecker(clients.engine),
        MigrationChecker(clients.engine, settings.migrations_dir),
        RedisChecker(clients),
        MQChecker(settings),
        ChromaChecker(settings, clients.http),
    ]
    return ReadinessService(checkers, timeout_s=settings.ready_timeout_ms / 1000)
