"""就绪检查聚合逻辑测试（Spec §9.2 / §17.6）。

用假 checker 覆盖四种结果：ok / fail / skipped / timeout，验证"不伪装健康"的语义。
"""

from __future__ import annotations

import asyncio

import pytest

from app.services.health import CheckResult, ReadinessService, _run_one


class FakeChecker:
    def __init__(
        self,
        name: str,
        *,
        enabled: bool = True,
        detail: str = "ok",
        error: Exception | None = None,
        delay: float = 0.0,
    ) -> None:
        self.name = name
        self.enabled = enabled
        self._detail = detail
        self._error = error
        self._delay = delay

    async def probe(self) -> str:
        if self._delay:
            await asyncio.sleep(self._delay)
        if self._error is not None:
            raise self._error
        return self._detail


async def test_run_one_ok() -> None:
    result = await _run_one(FakeChecker("mysql", detail="mysql 8.0.36"), timeout_s=1)
    assert result.status == "ok"
    assert result.detail == "mysql 8.0.36"
    assert result.healthy is True


async def test_run_one_fail_captures_exception_type() -> None:
    checker = FakeChecker("chroma", error=ConnectionError("refused"))
    result = await _run_one(checker, timeout_s=1)
    assert result.status == "fail"
    assert "ConnectionError: refused" in result.detail
    assert result.healthy is False


async def test_run_one_timeout() -> None:
    result = await _run_one(FakeChecker("redis", delay=0.5), timeout_s=0.01)
    assert result.status == "fail"
    assert "timeout" in result.detail


async def test_run_one_skipped_when_disabled() -> None:
    result = await _run_one(FakeChecker("mq", enabled=False), timeout_s=1)
    assert result.status == "skipped"
    assert result.detail == "disabled by config"
    assert result.healthy is True


async def test_service_reports_ready_with_skipped_dependency() -> None:
    service = ReadinessService(
        [FakeChecker("mysql", detail="up"), FakeChecker("mq", enabled=False)],
        timeout_s=1,
    )
    report = await service.check()
    assert report.ready is True
    assert [item.status for item in report.checks] == ["ok", "skipped"]
    payload = report.as_dict()
    assert payload["status"] == "ready"
    assert len(payload["checks"]) == 2


async def test_service_not_ready_when_any_check_fails() -> None:
    service = ReadinessService(
        [
            FakeChecker("mysql", detail="up"),
            FakeChecker("redis", error=RuntimeError("down")),
        ],
        timeout_s=1,
    )
    report = await service.check()
    assert report.ready is False
    assert report.as_dict()["status"] == "not_ready"


async def test_checks_run_concurrently() -> None:
    service = ReadinessService(
        [FakeChecker(f"c{i}", delay=0.1) for i in range(4)],
        timeout_s=1,
    )
    started = asyncio.get_running_loop().time()
    await service.check()
    elapsed = asyncio.get_running_loop().time() - started
    assert elapsed < 0.3, "就绪检查应并发执行，而非串行累加"


def test_check_result_dict_shape() -> None:
    result = CheckResult(name="mysql", status="ok", detail="up", latency_ms=3)
    assert result.as_dict() == {
        "name": "mysql",
        "status": "ok",
        "detail": "up",
        "latency_ms": 3,
    }


@pytest.mark.parametrize("status", ["ok", "fail", "skipped"])
def test_healthy_semantics(status: str) -> None:
    result = CheckResult(name="x", status=status)  # type: ignore[arg-type]
    assert result.healthy is (status != "fail")
