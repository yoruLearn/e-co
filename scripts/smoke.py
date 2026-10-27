"""M0 冒烟脚本：真起 uvicorn → 打探针/指标 → 退出。

CI 与本地 ``make smoke`` 共用；不依赖 MySQL/Redis，缺依赖时 ``/readyz`` 应为 503 且
明细可读（这正是 M0 的设计目标）。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from typing import Any

PORT = 8123
BASE = f"http://127.0.0.1:{PORT}"
STARTUP_TIMEOUT_SEC = 40


def _get(path: str) -> tuple[int, str]:
    try:
        with urllib.request.urlopen(f"{BASE}{path}", timeout=5) as response:  # noqa: S310
            return response.status, response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:  # 4xx/5xx 也要拿到 body
        return exc.code, exc.read().decode("utf-8")


def _wait_until_up() -> bool:
    deadline = time.time() + STARTUP_TIMEOUT_SEC
    while time.time() < deadline:
        try:
            status, _ = _get("/healthz")
            if status == 200:
                return True
        except (urllib.error.URLError, ConnectionError):
            pass
        time.sleep(0.5)
    return False


def main() -> int:
    env = {
        **os.environ,
        "APP_ENV": "dev",
        "DOCS_ENABLED": "false",
        "SHUTDOWN_DRAIN_SECONDS": "0",
        "LOG_LEVEL": "WARNING",
    }
    process = subprocess.Popen(  # noqa: S603 - 固定参数、开发自检脚本
        [
            sys.executable,
            "-m",
            "uvicorn",
            "app.main:app",
            "--port",
            str(PORT),
            "--log-level",
            "warning",
        ],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    failures: list[str] = []
    try:
        if not _wait_until_up():
            print(f"[smoke] 启动超时（{STARTUP_TIMEOUT_SEC}s）", file=sys.stderr)
            return 1

        health_status, health_body = _get("/healthz")
        health: dict[str, Any] = json.loads(health_body)
        print(f"[smoke] /healthz -> {health_status} code={health.get('code')}")
        if health_status != 200 or health.get("code") != 0:
            failures.append("/healthz 未返回 200/code=0")

        ready_status, ready_body = _get("/readyz")
        ready: dict[str, Any] = json.loads(ready_body)
        checks = (ready.get("data") or ready.get("detail") or {}).get("checks", [])
        print(f"[smoke] /readyz -> {ready_status} checks={[c.get('name') for c in checks]}")
        if ready_status not in (200, 503):
            failures.append(f"/readyz 状态码异常: {ready_status}")
        if not checks:
            failures.append("/readyz 未返回逐项检查明细")

        metrics_status, metrics_body = _get("/metrics")
        print(f"[smoke] /metrics -> {metrics_status}")
        if metrics_status != 200 or "http_requests_total" not in metrics_body:
            failures.append("/metrics 未暴露 http_requests_total")

        trace_header = None
        request = urllib.request.Request(  # noqa: S310 - 固定 http://127.0.0.1 地址
            f"{BASE}/healthz", headers={"X-Trace-Id": "tr_smoke0123456789abcdef"}
        )
        with urllib.request.urlopen(request, timeout=5) as response:  # noqa: S310
            trace_header = response.headers.get("X-Trace-Id")
        if trace_header != "tr_smoke0123456789abcdef":
            failures.append(f"trace 头未透传（实际 {trace_header!r}）")
    finally:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:  # pragma: no cover
            process.kill()

    if failures:
        print("[smoke] 失败：" + "; ".join(failures), file=sys.stderr)
        return 1
    print("[smoke] OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
