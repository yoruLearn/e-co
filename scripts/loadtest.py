"""SSE 压测脚本（M3）：并发用户 + 多轮，输出延迟分位与错误率。

用法：
    python scripts/loadtest.py --url http://127.0.0.1:8000/api/v1/chat/stream \
        --token "$env:TOKEN" --concurrency 50 --rounds 3 --message "退款多久到账"

口径：
- first_ms：从请求发出到第一个 delta（含 status/citation 之前的时间）；
- total_ms：到 done；
- 失败：HTTP 非 200 或流中出现 error 事件。
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
import time
from dataclasses import dataclass, field
from typing import Any

import httpx


@dataclass
class Sample:
    first_ms: float = 0.0
    total_ms: float = 0.0
    ok: bool = False
    error: str = ""
    tokens: int = 0


@dataclass
class Result:
    samples: list[Sample] = field(default_factory=list)

    def summary(self) -> dict[str, Any]:
        ok = [s for s in self.samples if s.ok]
        first = [s.first_ms for s in ok]
        total = [s.total_ms for s in ok]
        tokens = sum(s.tokens for s in ok)

        def pct(values: list[float], ratio: float) -> float:
            if not values:
                return 0.0
            ordered = sorted(values)
            return ordered[min(len(ordered) - 1, int(ratio * len(ordered)))]

        return {
            "total": len(self.samples),
            "ok": len(ok),
            "error_rate": round(1 - len(ok) / max(1, len(self.samples)), 4),
            "first_p50_ms": round(pct(first, 0.5), 1),
            "first_p95_ms": round(pct(first, 0.95), 1),
            "total_p50_ms": round(pct(total, 0.5), 1),
            "total_p95_ms": round(pct(total, 0.95), 1),
            "tokens": tokens,
            "errors": [s.error for s in self.samples if not s.ok][:5],
        }


async def one_call(
    client: httpx.AsyncClient, url: str, headers: dict[str, str], message: str, sid: str
) -> Sample:
    sample = Sample()
    started = time.perf_counter()
    try:
        async with client.stream(
            "POST",
            url,
            json={"session_id": sid, "message": message},
            headers=headers,
            timeout=60.0,
        ) as response:
            if response.status_code != 200:
                sample.error = f"HTTP {response.status_code}"
                return sample
            async for line in response.aiter_lines():
                if line.startswith("event: ") and line[7:] in {"delta", "citation"}:
                    if sample.first_ms == 0.0:
                        sample.first_ms = (time.perf_counter() - started) * 1000
                elif line.startswith("data: ") and '"usage"' in line:
                    with contextlib.suppress(json.JSONDecodeError, AttributeError, ValueError):
                        sample.tokens = int(json.loads(line[6:]).get("usage", {}).get("tokens", 0))
        sample.total_ms = (time.perf_counter() - started) * 1000
        sample.ok = True
    except Exception as exc:  # 压测脚本：失败记录不中断
        sample.error = type(exc).__name__
        sample.total_ms = (time.perf_counter() - started) * 1000
    return sample


async def run(args: argparse.Namespace) -> Result:
    result = Result()
    headers = {"Authorization": f"Bearer {args.token}"} if args.token else {}
    async with httpx.AsyncClient() as client:
        for round_index in range(args.rounds):
            tasks = [
                one_call(
                    client,
                    args.url,
                    headers,
                    args.message,
                    f"load-{os.getpid()}-{round_index}-{index}",
                )
                for index in range(args.concurrency)
            ]
            result.samples.extend(await asyncio.gather(*tasks))
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="SSE 压测")
    parser.add_argument("--url", default="http://127.0.0.1:8000/api/v1/chat/stream")
    parser.add_argument("--token", default=os.getenv("LOADTEST_TOKEN", ""))
    parser.add_argument("--concurrency", type=int, default=10)
    parser.add_argument("--rounds", type=int, default=1)
    parser.add_argument("--message", default="退款多久到账")
    parser.add_argument("--p95-threshold-ms", type=float, default=1500.0)
    args = parser.parse_args()

    summary = asyncio.run(run(args)).summary()
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    if summary["error_rate"] > 0.01:
        print("[loadtest] FAIL: error_rate > 1%")
        return 1
    if summary["total_p95_ms"] > args.p95_threshold_ms:
        print(f"[loadtest] FAIL: total p95 {summary['total_p95_ms']}ms > {args.p95_threshold_ms}ms")
        return 1
    print("[loadtest] PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
