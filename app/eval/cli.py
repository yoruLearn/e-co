"""评测 CLI（Spec §13.3）。

# 离线跑（内存索引 + 当前配置的模型/embedding；本机无 Docker 时可用）
python -m app.eval.cli run --offline --suite local --limit 20

# 在线跑（真实 MySQL + Chroma；staging/CI）
python -m app.eval.cli run --suite nightly

# 只看门禁结果、不进 CI：退出码 0
python -m app.eval.cli run --offline --no-fail-on-gate
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any
from uuid import uuid4

from app.common.clients import build_clients
from app.common.config import Settings, get_settings
from app.common.logging import configure_logging, get_logger
from app.eval.cases import EvalCase, EvalCategory, count_by_category, load_cases
from app.eval.executor import filter_cases, run_suite
from app.eval.graph_executor import GraphCaseExecutor
from app.eval.offline import build_offline_runtime
from app.eval.runner import GateThresholds
from app.eval.store import save_report

logger = get_logger("app.eval.cli")


async def _run_online(settings: Settings, cases: list[EvalCase], suite: str) -> dict[str, Any]:
    from app.agent.runtime import build_agent_runtime, init_agent_runtime

    clients = build_clients(settings)
    try:
        runtime = build_agent_runtime(settings, clients)
        await init_agent_runtime(runtime)
        try:
            report = await run_suite(
                run_id=f"ev_{uuid4().hex[:12]}",
                suite=suite,
                cases=cases,
                executor=GraphCaseExecutor(runtime.graph),
                thresholds=GateThresholds(),
            )
            await save_report(clients.session_factory, report)
            return report.as_dict()
        finally:
            await runtime.close()
    finally:
        await clients.aclose()


async def _run_offline(settings: Settings, cases: list[EvalCase], suite: str) -> dict[str, Any]:
    runtime = await build_offline_runtime(settings)
    try:
        report = await run_suite(
            run_id=f"ev_{uuid4().hex[:12]}",
            suite=suite,
            cases=cases,
            executor=GraphCaseExecutor(runtime.graph),
            thresholds=GateThresholds(),
        )
        return report.as_dict()
    finally:
        await runtime.close()


def _parse_categories(raw: str | None) -> list[EvalCategory] | None:
    if not raw:
        return None
    result = []
    for item in raw.split(","):
        value = item.strip()
        if not value:
            continue
        try:
            result.append(EvalCategory(value))
        except ValueError as exc:
            raise SystemExit(f"未知类别：{value}，可选 {[c.value for c in EvalCategory]}") from exc
    return result


async def _amain(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.eval.cli", description="评测执行器")
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run", help="跑评测集")
    run.add_argument("--suite", default="local")
    run.add_argument("--cases-dir", default=None)
    run.add_argument(
        "--categories", default=None, help="逗号分隔：typical,multi_hop,no_answer,multi_turn"
    )
    run.add_argument("--limit", type=int, default=None, help="只跑前 N 条（冒烟用）")
    run.add_argument("--offline", action="store_true", help="内存索引，不依赖 MySQL/Chroma")
    run.add_argument("--json", action="store_true", help="输出完整 JSON 报告")
    run.add_argument("--no-fail-on-gate", action="store_true", help="门禁失败也返回 0")
    args = parser.parse_args(argv)

    settings = get_settings()
    configure_logging(settings.log_level, "console")
    cases_dir = Path(args.cases_dir or settings.eval_cases_dir)
    cases = filter_cases(load_cases(cases_dir), _parse_categories(args.categories))
    if args.limit:
        cases = cases[: args.limit]
    if not cases:
        print(f"[eval] 没有用例：{cases_dir}", file=sys.stderr)
        return 1
    print(
        f"[eval] cases={len(cases)} distribution={count_by_category(cases)}",
        file=sys.stderr,
    )

    report = (
        await _run_offline(settings, cases, args.suite)
        if args.offline
        else await _run_online(settings, cases, args.suite)
    )
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(
            "[eval] "
            f"passed={report['passed']}/{report['total']} "
            f"completion={report['completion_rate']:.2%} "
            f"citation_acc={report['citation_accuracy']:.2%} "
            f"p95={report['p95_ms']}ms "
            f"duration={report['duration_ms']}ms"
        )
        print(f"[eval] attribution={report['attribution']}")
        gate = report.get("gate") or {}
        print(f"[eval] gate_passed={gate.get('passed')} failures={gate.get('failures')}")
        for item in (report.get("failures") or [])[:10]:
            print(
                f"  - {item['case_key']} [{item['category']}] {item['attribution']}: "
                f"{'; '.join(item['failures'])}"
            )
    gate = report.get("gate") or {}
    if not gate.get("passed") and not args.no_fail_on_gate:
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    return asyncio.run(_amain(argv))


if __name__ == "__main__":
    sys.exit(main())
