"""工具注册表测试（Spec §8.1）。"""

from __future__ import annotations

import pytest

from app.tools.registry import (
    READ_ONLY_TOOL_NAMES,
    TOOL_MAP,
    TOOL_SPECS,
    WRITE_TOOL_NAMES,
    ToolLevel,
    get_tool,
    missing_params,
)


def test_seven_tools_registered() -> None:
    assert len(TOOL_SPECS) == 7
    assert len(TOOL_MAP) == 7
    assert len(set(TOOL_MAP)) == 7


def test_tool_split_matches_spec() -> None:
    assert {
        "get_order",
        "get_logistics",
        "get_stock",
        "get_refund_progress",
        "check_aftersales_policy",
    } == READ_ONLY_TOOL_NAMES
    assert {"create_after_sales", "cancel_after_sales"} == WRITE_TOOL_NAMES


def test_timeouts_match_spec_table() -> None:
    expected = {
        "get_order": 800,
        "get_logistics": 800,
        "get_stock": 500,
        "get_refund_progress": 800,
        "check_aftersales_policy": 1200,
        "create_after_sales": 1000,
        "cancel_after_sales": 1000,
    }
    assert {spec.name: spec.timeout_ms for spec in TOOL_SPECS} == expected


def test_write_tools_require_confirm_and_idempotency() -> None:
    for name in WRITE_TOOL_NAMES:
        spec = TOOL_MAP[name]
        assert spec.level is ToolLevel.WRITE
        assert spec.requires_confirm is True
        assert spec.idempotent is True
        assert spec.audit is True


def test_read_only_tools_never_require_confirm() -> None:
    for name in READ_ONLY_TOOL_NAMES:
        assert TOOL_MAP[name].requires_confirm is False


def test_required_params_match_spec() -> None:
    assert TOOL_MAP["get_order"].required_params == ("order_id",)
    assert TOOL_MAP["get_stock"].required_params == ("sku_id", "qty")
    assert TOOL_MAP["check_aftersales_policy"].required_params == ("category", "days")
    assert TOOL_MAP["create_after_sales"].required_params == ("order_id", "action", "reason")
    assert TOOL_MAP["create_after_sales"].optional_params == ("sku_id", "amount")


def test_every_tool_has_degrade_policy() -> None:
    for spec in TOOL_SPECS:
        assert spec.degrade, f"{spec.name} 缺少降级策略"


def test_get_tool_unknown_returns_none() -> None:
    assert get_tool("delete_everything") is None
    assert get_tool("get_order") is not None


@pytest.mark.parametrize(
    ("slots", "expected"),
    [
        ({}, ["order_id"]),
        ({"order_id": "O1"}, []),
        ({"order_id": ""}, ["order_id"]),
        ({"order_id": None}, ["order_id"]),
        ({"order_id": "  "}, ["order_id"]),
    ],
)
def test_missing_params(slots: dict[str, object], expected: list[str]) -> None:
    assert missing_params(TOOL_MAP["get_order"], slots) == expected


def test_missing_params_reports_all_required() -> None:
    assert missing_params(TOOL_MAP["get_stock"], {"sku_id": "S1"}) == ["qty"]
    assert missing_params(TOOL_MAP["get_stock"], {}) == ["sku_id", "qty"]
