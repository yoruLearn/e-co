"""pytest 公共夹具。

原则：测试**不连真实依赖**。``Settings`` 指向必然拒绝连接的端口，并把超时压到 100ms，
这样 ``/readyz`` 的降级路径本身成为可测行为（M0 的核心设计目标之一）。
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.common.config import Settings
from app.common.logging import clear_log_context
from app.common.security import Role, create_access_token
from app.common.trace import reset_trace_context
from app.main import create_app


@pytest.fixture(autouse=True)
def _isolate_contexts() -> Iterator[None]:
    """每个测试前后清空 contextvars，避免 trace/日志上下文跨用例泄漏。"""
    reset_trace_context()
    clear_log_context()
    yield
    reset_trace_context()
    clear_log_context()


@pytest.fixture(scope="session")
def settings() -> Settings:
    return Settings(
        _env_file=None,
        app_env="test",
        log_level="WARNING",
        log_format="console",
        database_url="mysql+aiomysql://u:p@127.0.0.1:1/aftersales",
        redis_url="redis://127.0.0.1:1/0",
        chroma_url="http://127.0.0.1:1",
        ready_timeout_ms=100,
        shutdown_drain_seconds=0,
        mq_enabled=False,
        docs_enabled=True,
        rag_offline=True,
    )


@pytest.fixture()
def app(settings: Settings) -> Any:
    return create_app(settings)


@pytest.fixture()
def client(app: Any) -> Iterator[TestClient]:
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture()
def make_auth(settings: Settings) -> Any:
    """生成 ``Authorization`` 头。"""

    def _make(role: Role | str = Role.BUYER, user_id: str = "u_test") -> dict[str, str]:
        token, _ = create_access_token(user_id=user_id, role=Role(role), settings=settings)
        return {"Authorization": f"Bearer {token}"}

    return _make


@pytest.fixture()
def buyer_auth(make_auth: Any) -> dict[str, str]:
    headers: dict[str, str] = make_auth(Role.BUYER)
    return headers


@pytest.fixture()
def agent_auth(make_auth: Any) -> dict[str, str]:
    headers: dict[str, str] = make_auth(Role.AGENT)
    return headers


@pytest.fixture()
def ops_auth(make_auth: Any) -> dict[str, str]:
    headers: dict[str, str] = make_auth(Role.OPS)
    return headers


@pytest.fixture()
def admin_auth(make_auth: Any) -> dict[str, str]:
    headers: dict[str, str] = make_auth(Role.ADMIN)
    return headers
