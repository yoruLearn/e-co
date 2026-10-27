"""worker 冒烟测试：单次心跳、心跳内容、Redis 不可用时不崩溃。"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from app.common.config import Settings
from app.workers.runner import (
    heartbeat_key,
    heartbeat_payload,
    parse_args,
    run_worker,
    write_heartbeat,
)


@pytest.fixture()
def settings() -> Settings:
    return Settings(
        _env_file=None,
        app_env="test",
        database_url="mysql+aiomysql://u:p@127.0.0.1:1/x",
        redis_url="redis://127.0.0.1:1/0",
        chroma_url="http://127.0.0.1:1",
        ready_timeout_ms=100,
        log_level="WARNING",
    )


def test_parse_args_once_flag() -> None:
    assert parse_args(["--once"]).once is True
    assert parse_args([]).once is False


def test_heartbeat_key_contains_hostname() -> None:
    assert heartbeat_key("host-a") == "worker:heartbeat:host-a"


def test_heartbeat_payload_is_json(settings: Settings) -> None:
    payload = json.loads(heartbeat_payload(settings=settings, started_at=0))
    assert payload["role"] == "worker"
    assert payload["env"] == "test"
    assert payload["version"] == settings.app_version
    assert payload["pid"] > 0


async def test_write_heartbeat_ok(settings: Settings) -> None:
    calls: list[tuple[str, str, int | None]] = []

    class FakeRedis:
        async def set(self, key: str, value: str, ex: int | None = None) -> bool:
            calls.append((key, value, ex))
            return True

    clients = SimpleNamespace(redis=FakeRedis())
    assert await write_heartbeat(clients, settings, started_at=0) is True  # type: ignore[arg-type]
    assert len(calls) == 1
    key, value, ex = calls[0]
    assert key.startswith("worker:heartbeat:")
    assert ex == settings.worker_heartbeat_ttl_sec
    assert json.loads(value)["role"] == "worker"


async def test_write_heartbeat_failure_is_swallowed(settings: Settings) -> None:
    class BrokenRedis:
        async def set(self, *_args: object, **_kwargs: object) -> bool:
            raise ConnectionError("redis down")

    clients = SimpleNamespace(redis=BrokenRedis())
    assert await write_heartbeat(clients, settings, started_at=0) is False  # type: ignore[arg-type]


def test_run_worker_once_exits_zero(settings: Settings) -> None:
    """Redis 不可达时 worker 仍应正常退出（心跳失败只告警）。"""
    exit_code = asyncio.run(run_worker(settings=settings, once=True, stop=asyncio.Event()))
    assert exit_code == 0
