"""worker 入口（Spec §17.6：与 api 同镜像、不同入口命令）。

M0 能力：结构化日志 + Redis 心跳 + 优雅退出。消费者（工单落库、审批回调、通知、
索引构建、评测执行）在 M2/M3 接入，见 ``app/workers/consumers.py``。

用法::

    python -m app.workers.runner            # 常驻
    python -m app.workers.runner --once     # 单次心跳（CI 冒烟 / 探活）
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import signal
import socket
import sys
import time
from collections.abc import Sequence

from app.common.clients import AppClients, build_clients
from app.common.config import Settings, get_settings
from app.common.logging import configure_logging, update_log_context
from app.common.trace import trace_scope

logger = logging.getLogger("app.worker")

HEARTBEAT_KEY_PREFIX = "worker:heartbeat:"


def heartbeat_key(hostname: str | None = None) -> str:
    return f"{HEARTBEAT_KEY_PREFIX}{hostname or socket.gethostname()}"


def heartbeat_payload(*, settings: Settings, started_at: float) -> str:
    return json.dumps(
        {
            "role": "worker",
            "env": settings.app_env,
            "version": settings.app_version,
            "host": socket.gethostname(),
            "pid": os.getpid(),
            "uptime_s": round(time.time() - started_at, 3),
        },
        ensure_ascii=False,
    )


async def write_heartbeat(clients: AppClients, settings: Settings, started_at: float) -> bool:
    """写 Redis 心跳；Redis 不可用时**只告警不崩溃**（worker 可降级空跑）。"""
    key = heartbeat_key()
    payload = heartbeat_payload(settings=settings, started_at=started_at)
    try:
        await clients.redis.set(key, payload, ex=settings.worker_heartbeat_ttl_sec)
    except Exception as exc:
        logger.warning(
            "worker.heartbeat.failed",
            extra={"key": key, "reason": type(exc).__name__, "detail": str(exc)[:200]},
        )
        return False
    logger.info("worker.heartbeat.ok", extra={"key": key})
    return True


def _install_signal_handlers(stop: asyncio.Event) -> None:
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except (NotImplementedError, AttributeError):
            # Windows 的 ProactorEventLoop 不支持 add_signal_handler，退回 signal.signal
            def _handler(*_args: object, _stop: asyncio.Event = stop) -> None:
                loop.call_soon_threadsafe(_stop.set)

            signal.signal(sig, _handler)


async def run_worker(*, settings: Settings, once: bool, stop: asyncio.Event) -> int:
    started_at = time.time()
    clients = build_clients(settings)
    beats = 0
    try:
        while not stop.is_set():
            with trace_scope(session_id=None) as trace_id:
                ok = await write_heartbeat(clients, settings, started_at)
                beats += 1
                logger.info("worker.tick", extra={"trace_id": trace_id, "ok": ok})
            if once:
                break
            try:
                await asyncio.wait_for(stop.wait(), timeout=settings.worker_heartbeat_interval_sec)
            except TimeoutError:
                continue
    finally:
        await clients.aclose()
        logger.info("worker.stopped", extra={"beats": beats})
    return 0


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="电商售后管家 worker")
    parser.add_argument("--once", action="store_true", help="只跑一次心跳后退出（冒烟用）")
    return parser.parse_args(argv)


async def _amain(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    settings = get_settings()
    configure_logging(settings.log_level, settings.log_format, force=True)
    update_log_context(role="worker", env=settings.app_env, release=settings.release_channel)
    stop = asyncio.Event()
    _install_signal_handlers(stop)
    logger.info("worker.start", extra={"once": bool(args.once), "env": settings.app_env})
    return await run_worker(settings=settings, once=bool(args.once), stop=stop)


def main(argv: Sequence[str] | None = None) -> int:
    try:
        return asyncio.run(_amain(argv))
    except KeyboardInterrupt:  # pragma: no cover - 交互式中断
        return 130


if __name__ == "__main__":
    sys.exit(main())
