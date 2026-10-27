"""API 集成测试：探针、信封、鉴权、501 契约、SSE、OpenAPI。

全部使用 TestClient（进程内 ASGI），不依赖任何外部组件。
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.main import create_app

TRACE_HEADER = "X-Trace-Id"

#: Spec §9.2 接口清单（M0 必须全部出现在 OpenAPI 中）
EXPECTED_PATHS = {
    "/healthz",
    "/readyz",
    "/api/v1/chat/stream",
    "/api/v1/aftersales/{biz_id}",
    "/api/v1/aftersales/apply",
    "/api/v1/aftersales/{biz_id}/cancel",
    "/api/v1/approval/callback",
    "/api/v1/eval/run",
    "/api/v1/traces/{trace_id}",
    "/api/v1/auth/login",
    "/api/v1/orders/{order_id}",
    "/api/v1/admin/prompts/{key}/publish",
    "/api/v1/admin/index/{version}/activate",
    "/api/v1/admin/breaker",
    "/api/v1/admin/gray",
    "/api/v1/admin/dashboard",
    "/api/v1/admin/sessions/{session_id}/takeover",
    "/api/v1/admin/audit",
}


def test_openapi_exposes_every_spec_path(client: TestClient) -> None:
    schema = client.get("/openapi.json").json()
    paths = set(schema["paths"])
    assert paths >= EXPECTED_PATHS, f"缺失接口: {EXPECTED_PATHS - paths}"


def test_healthz_returns_envelope_and_is_dependency_free(client: TestClient) -> None:
    response = client.get("/healthz")
    assert response.status_code == 200
    body = response.json()
    assert body["code"] == 0
    assert body["message"] == "ok"
    assert body["data"]["status"] == "ok"
    assert body["data"]["env"] == "test"
    assert body["trace_id"]
    assert response.headers[TRACE_HEADER] == body["trace_id"]


def test_readyz_is_503_with_per_check_details(client: TestClient) -> None:
    response = client.get("/readyz")
    assert response.status_code == 503
    body = response.json()
    assert body["code"] == "AS-5032"
    checks = body["detail"]["checks"]
    names = {check["name"] for check in checks}
    assert names == {"mysql", "migrations", "redis", "mq", "chroma"}
    by_name = {check["name"]: check for check in checks}
    assert by_name["mysql"]["status"] == "fail"
    assert by_name["redis"]["status"] == "fail"
    assert by_name["mq"]["status"] == "skipped"
    assert by_name["chroma"]["status"] == "fail"
    assert all(check["latency_ms"] >= 0 for check in checks)


def test_trace_header_is_passed_through_when_valid(client: TestClient) -> None:
    trace_id = "tr_custom0123456789ab"
    response = client.get("/healthz", headers={TRACE_HEADER: trace_id})
    assert response.headers[TRACE_HEADER] == trace_id
    assert response.json()["trace_id"] == trace_id


@pytest.mark.parametrize("bad_trace", ["x", "has space in it", "a" * 100])
def test_invalid_trace_header_is_replaced(client: TestClient, bad_trace: str) -> None:
    response = client.get("/healthz", headers={TRACE_HEADER: bad_trace})
    assert response.status_code == 200
    replacement = response.headers[TRACE_HEADER]
    assert replacement != bad_trace
    assert 16 <= len(replacement) <= 64


def test_metrics_endpoint_exposes_http_metrics(client: TestClient) -> None:
    client.get("/healthz")
    response = client.get("/metrics")
    assert response.status_code == 200
    assert "http_requests_total" in response.text
    assert "app_info" in response.text


def test_dev_login_issues_usable_token(client: TestClient) -> None:
    response = client.post("/api/v1/auth/login", json={"user_id": "u_admin", "role": "admin"})
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["token_type"] == "bearer"
    assert data["expires_in"] > 0

    dashboard = client.get(
        "/api/v1/admin/dashboard",
        headers={"Authorization": f"Bearer {data['access_token']}"},
    )
    # 角色通过校验后落到"未实现"分支
    assert dashboard.status_code == 501
    assert dashboard.json()["code"] == "AS-5002"


def test_protected_endpoint_requires_token(client: TestClient) -> None:
    response = client.get("/api/v1/admin/dashboard")
    assert response.status_code == 401
    assert response.json()["code"] == "AS-4010"


def test_invalid_token_is_401(client: TestClient) -> None:
    response = client.get("/api/v1/admin/dashboard", headers={"Authorization": "Bearer not-a-jwt"})
    assert response.status_code == 401
    assert response.json()["code"] == "AS-4010"


def test_role_enforcement_returns_403(client: TestClient, buyer_auth: dict[str, str]) -> None:
    response = client.get("/api/v1/admin/dashboard", headers=buyer_auth)
    assert response.status_code == 403
    body = response.json()
    assert body["code"] == "AS-4030"
    assert body["detail"]["actual_role"] == "buyer"


def test_login_validation_error_is_as_4001(client: TestClient) -> None:
    response = client.post("/api/v1/auth/login", json={"user_id": "", "role": "buyer"})
    assert response.status_code == 400
    body = response.json()
    assert body["code"] == "AS-4001"
    assert body["detail"]["errors"]


def test_unknown_route_returns_as_4040(client: TestClient) -> None:
    response = client.get("/api/v1/does-not-exist")
    assert response.status_code == 404
    assert response.json()["code"] == "AS-4040"


@pytest.mark.parametrize(
    ("method", "path", "auth_fixture", "payload"),
    [
        ("get", "/api/v1/aftersales/AS20261027001", "buyer_auth", None),
        ("post", "/api/v1/aftersales/AS20261027001/cancel", "buyer_auth", {}),
        ("get", "/api/v1/orders/O2026102588", "buyer_auth", None),
        (
            "post",
            "/api/v1/approval/callback",
            None,
            {
                "approval_task_id": "ap_1",
                "decision": "approve",
                "approver": "u_889",
            },
        ),
        ("get", "/api/v1/traces/tr_0123456789abcdef", "agent_auth", None),
        ("get", "/api/v1/admin/dashboard", "admin_auth", None),
        ("post", "/api/v1/admin/breaker", "admin_auth", {"scope": "write", "open": True}),
        ("post", "/api/v1/admin/gray", "admin_auth", {"percent": 10}),
        ("post", "/api/v1/admin/prompts/router.v3/publish", "admin_auth", {"version": 6}),
        ("post", "/api/v1/admin/index/v3/activate", "admin_auth", {}),
        ("post", "/api/v1/admin/sessions/sess_1/takeover", "admin_auth", {}),
        ("get", "/api/v1/admin/audit", "admin_auth", None),
    ],
)
def test_unimplemented_endpoints_return_501(
    client: TestClient,
    request: pytest.FixtureRequest,
    method: str,
    path: str,
    auth_fixture: str | None,
    payload: dict[str, Any] | None,
) -> None:
    headers: dict[str, str] = {}
    if auth_fixture:
        headers.update(request.getfixturevalue(auth_fixture))
    response = client.request(method.upper(), path, json=payload, headers=headers)
    assert response.status_code == 501, response.text
    body = response.json()
    assert body["code"] == "AS-5002"
    assert body["detail"]["milestone"]
    assert body["trace_id"]


def test_apply_after_sales_requires_confirm_token(
    client: TestClient, buyer_auth: dict[str, str]
) -> None:
    payload = {
        "order_id": "O2026102588",
        "action": "refund",
        "reason": "quality_issue",
        "amount": 199.0,
        "sku_id": "SKU123",
    }
    response = client.post("/api/v1/aftersales/apply", json=payload, headers=buyer_auth)
    assert response.status_code == 400
    assert response.json()["code"] == "AS-4001"


def test_apply_after_sales_with_confirm_and_idempotency_key(
    client: TestClient, buyer_auth: dict[str, str]
) -> None:
    payload = {
        "order_id": "O2026102588",
        "action": "refund",
        "reason": "quality_issue",
        "amount": 199.0,
        "confirm_token": "ct_9f3a",
    }
    headers = {**buyer_auth, "Idempotency-Key": "AS20261027001:create"}
    response = client.post("/api/v1/aftersales/apply", json=payload, headers=headers)
    assert response.status_code == 501
    assert response.json()["code"] == "AS-5002"


def test_apply_rejects_bad_idempotency_key(client: TestClient, buyer_auth: dict[str, str]) -> None:
    payload = {
        "order_id": "O1",
        "action": "refund",
        "reason": "quality_issue",
        "confirm_token": "ct_1",
    }
    headers = {**buyer_auth, "Idempotency-Key": "bad key!"}
    response = client.post("/api/v1/aftersales/apply", json=payload, headers=headers)
    assert response.status_code == 400
    assert response.json()["code"] == "AS-4001"


def test_chat_stream_emits_sse_frames(client: TestClient, buyer_auth: dict[str, str]) -> None:
    with client.stream(
        "POST",
        "/api/v1/chat/stream",
        json={"session_id": "sess_1", "message": "退款多久到账"},
        headers=buyer_auth,
    ) as response:
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        assert response.headers["cache-control"] == "no-cache"
        body = b"".join(response.iter_bytes()).decode("utf-8")

    frames = [frame for frame in body.split("\n\n") if frame.strip()]
    events = [
        line.removeprefix("event: ")
        for frame in frames
        for line in frame.splitlines()
        if line.startswith("event: ")
    ]
    # 离线（无 Chroma/MySQL/LLM 录制）时应走：router 规则兜底 → 检索兜底 → 澄清 → done
    assert events[0] == "status"
    assert "delta" in events
    assert "clarify" in events
    assert events[-1] == "done"
    done_payload = json.loads(frames[-1].split("data: ", 1)[1])
    assert done_payload["trace_id"]
    assert all(frame.splitlines()[0].startswith("id: ") for frame in frames)


def test_unhandled_exception_is_masked(
    settings: Any,
) -> None:
    app = create_app(settings)

    @app.get("/boom")
    async def boom() -> None:
        raise RuntimeError("boom")

    with TestClient(app, raise_server_exceptions=False) as test_client:
        response = test_client.get("/boom")
    assert response.status_code == 500
    body = response.json()
    assert body["code"] == "AS-5001"
    assert "RuntimeError" not in response.text
    assert "boom" not in response.text
