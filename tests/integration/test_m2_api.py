"""M2 接口测试：订单只读、审批验签、写接口前置校验（不依赖 MySQL）。

DB 相关路径（apply/detail/cancel 成功态）由 CI 的 MySQL job 覆盖。
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.common.security import Role


def test_order_query_own_order(make_auth, client: TestClient) -> None:
    headers = make_auth(Role.BUYER, "u_buyer_1")
    response = client.get("/api/v1/orders/O20261027001", headers=headers)
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["order_id"] == "O20261027001"
    assert data["lines"][0]["sku_id"] == "SKU-BT-PRO"


def test_order_query_other_buyer_is_forbidden(make_auth, client: TestClient) -> None:
    headers = make_auth(Role.BUYER, "u_buyer_2")
    response = client.get("/api/v1/orders/O20261027001", headers=headers)
    assert response.status_code == 403
    assert response.json()["code"] == "AS-4030"


def test_order_query_unknown_is_404(make_auth, client: TestClient) -> None:
    headers = make_auth(Role.BUYER, "u_buyer_1")
    response = client.get("/api/v1/orders/O404", headers=headers)
    assert response.status_code == 404
    assert response.json()["code"] == "AS-4040"


def test_approval_callback_rejects_bad_signature(client: TestClient) -> None:
    response = client.post(
        "/api/v1/approval/callback",
        json={
            "approval_task_id": "ap_1",
            "decision": "approve",
            "approver": "u_889",
        },
        headers={"X-Signature": "deadbeef"},
    )
    assert response.status_code == 401
    assert response.json()["code"] == "AS-4010"


def test_apply_requires_idempotency_key(make_auth, client: TestClient) -> None:
    headers = make_auth(Role.BUYER, "u_buyer_1")
    response = client.post(
        "/api/v1/aftersales/apply",
        json={
            "order_id": "O20261027001",
            "action": "refund",
            "reason": "quality_issue",
            "amount": 199.0,
            "confirm_token": "ct_x",
        },
        headers=headers,
    )
    assert response.status_code == 400
    assert response.json()["code"] == "AS-4001"


def test_aftersales_detail_requires_auth(client: TestClient) -> None:
    response = client.get("/api/v1/aftersales/AS20261027001")
    assert response.status_code == 401
