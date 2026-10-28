# M2 验证记录 · 办事链路

> 逐轮留痕；数字以本地/CI 实际输出为准。

## 第 1 天（2026-10-28）：方案 + 状态机/幂等/双确认/工具/审批/API 第一刀

| # | 命令 | 结果 |
| --- | --- | --- |
| 1 | `ruff check .` / `ruff format --check .` | ✅ 163 文件 |
| 2 | `mypy app`（strict） | ✅ 94 source files |
| 3 | `pytest -q --cov=app` | ✅ **556 passed, 10 skipped，82.77%** |
| 4 | `pytest tests/unit/test_m2_services.py` | ✅ 12 例（令牌/幂等/HMAC/阈值/工具） |
| 5 | `pytest tests/integration/test_m2_api.py` | ✅ 6 例（订单越权、404、验签 401、缺幂等头 400） |

### 本轮落地

| 能力 | 落点 | 说明 |
| --- | --- | --- |
| 状态机唯一入口 | `app/services/transition.py` | 迁移表校验 + 乐观锁 UPDATE + 流水，冲突 `AS-4003` |
| 售后单服务 | `app/services/aftersales.py` | apply/get/cancel；`biz+transition+approval_task+outbox` 同事务 |
| 幂等 | `app/services/idempotency_store.py` | Redis SET NX + 参数指纹 + 首次响应重放；DB `uk_idem` 兜底 |
| 双确认 | `app/services/confirm.py` | 单次消费（GETDEL）、5 分钟、绑定人/动作/订单/金额；Redis 故障失败关闭 |
| 只读工具 | `app/tools/orders.py` | 订单/物流/退款/库存 + 800/500ms 超时降级 + buyer 越权 403 |
| 审批 | `app/services/approval.py` | >500 建任务、HMAC-SHA256 验签、`callback_count` + 非 pending 幂等返回、outbox |
| API | `routers/aftersales.py`、`approval.py`、`orders.py` | apply/detail/cancel/callback/orders 接线 |

### 尚未验证（不许算绿）

| 项 | 原因 | 何时补 |
| --- | --- | --- |
| 并发 10 次重复提交 → 1 单 | 本机无 MySQL（Docker Hub 不可达） | M2-10 CI 加 MySQL service job |
| 非法迁移全表用例/版本冲突 | 需真实 UPDATE rowcount | 同上 |
| 审批回调幂等（重复回调同结果） | 需 DB 任务状态 | 同上 |
| 审批挂起后恢复 | 需图接入（M2-08） | 下一轮 |
| tool_audit 写审计 | 尚未接写工具 | M2-05 收尾 |

### 本轮修掉的问题

| # | 问题 | 修复 |
| --- | --- | --- |
| 1 | 冻结 slots dataclass 没有 `__dict__`，确认令牌签发直接 503 | 改 `dataclasses.asdict` |
| 2 | `ToolResult.data` 标成 dict，但工具返回 Order/list/int，mypy 报错 | 改 `Any`（序列化在边界做） |
| 3 | 订单行 schema 要 `amount`，工具只给了单价 | 工具补行小计 |
| 4 | 501 契约测试列表仍包含已实现端点 | 移除并改由真实接口测试覆盖 |


## 第 2 天（2026-10-28）：图接入 + 写工具审计 + outbox

| # | 命令 | 结果 |
| --- | --- | --- |
| 1 | `ruff check .` / `ruff format --check .` | ✅ 172 文件 |
| 2 | `mypy app`（strict） | ✅ 98 source files |
| 3 | `pytest -q --cov=app` | ✅ **559 passed, 10 skipped，81.18%** |
| 4 | `pytest tests/integration/test_chat_aftersales.py` | ✅ 3 例（两轮确认/进度查询/缺订单号澄清） |

### 本轮落地

| 能力 | 落点 | 说明 |
| --- | --- | --- |
| 图接入 | `nodes/core.py` 的 `intake` / `query_status` | after_sales：取参 → 签 confirm_token；带 token 的下一轮 → 写工具受理；query：按 AS 单号读 DB |
| 跨轮状态 | `app/session/store.py` | `pending_action` 随会话持久化（Redis），重启/多标签不丢 |
| 写工具 + 审计 | `app/tools/aftersales.py`、`services/tool_audit.py` | 单次消费 token → 状态机 → `tool_audit`（params_hash/result_code/cost_ms） |
| outbox 投递 | `services/outbox_dispatcher.py` | pending 轮询 → published；失败指数退避 5→300s；>5 次 dead + `dlq_total` |
| outbox 消费 | `services/outbox_consumer.py` | Redis `SET NX consumed:{msg_id}` 去重 + 标记 consumed |

### 尚未验证

| 项 | 原因 | 计划 |
| --- | --- | --- |
| MySQL 并发幂等（10 → 1）、非法迁移全表、回调幂等 | 本机无 Docker/MySQL | M2-10 CI service job |
| 图的两轮确认走真实 API（含 SSE confirm 事件） | 需要 DB + Redis 完整链路 | M2-10 |
| 审批通过后的下一轮状态回溯 | 同上 | M2-10 |

