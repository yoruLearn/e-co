"""端到端用例占位（M2 起启用）。

M0 阶段没有可端到端的业务链路（对话/办事均为 501），因此这里用 skip 明确标注归属，
而不是写一个假通过的测试。
"""

from __future__ import annotations

import pytest


@pytest.mark.e2e
def test_end_to_end_refund_flow_planned_for_m2() -> None:
    pytest.skip("端到端办单流程（对话 → 双确认 → 审批 → 落库）计划在 M2 交付")
