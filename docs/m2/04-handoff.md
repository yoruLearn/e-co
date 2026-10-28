# M2 交接文档 · 办事链路

> 最后更新：2026-10-28 · 对应提交见文末。

## 1. 状态

| 里程碑 | 状态 |
| --- | --- |
| M2-00 方案/工单 | ✅ |
| M2-01 状态机唯一入口 | ✅ |
| M2-02 幂等（Redis + DB 唯一键） | ✅ |
| M2-03 双确认令牌 | ✅ |
| M2-04 只读工具 | ✅ |
| M2-05 写工具 + 审计 | ✅ |
| M2-06 审批（HMAC/幂等/outbox） | ✅ |
| M2-07 API 接线 | ✅ |
| M2-08 图接入（两轮确认/进度查询） | ✅ |
| M2-09 outbox 投递/消费 | ✅ |
| M2-10 MySQL 集成 + CI job | ✅ CI 7/7 |
| M2-11 文档 | ✅ |

## 2. 关键落点

```text
app/services/transition.py      状态迁移唯一入口（乐观锁 + 流水）
app/services/aftersales.py      受理/查询/取消（biz+transition+approval+outbox 同事务）
app/services/confirm.py         双确认令牌（GETDEL 单次、5min、绑定参数）
app/services/idempotency_store.py Redis 幂等 + 首次响应重放
app/services/approval.py        HMAC 回调 + 幂等 + 状态推进
app/services/outbox_dispatcher.py  投递 + 退避 + 死信
app/services/outbox_consumer.py    消费去重 + 标记 consumed
app/tools/orders.py / aftersales.py 只读/写工具 + tool_audit
app/agent/nodes/core.py         intake / query_status 节点
docs/m2/*                       方案、工单、验证、本文件
```

## 3. 已知边界（下一阶段）

| 项 | 说明 |
| --- | --- |
| 真实订单 Provider | Mock fixtures；M4 联调真实订单/物流/退款接口 |
| 审批挂起跨进程恢复 | 以 DB 状态为准；图为一次请求内执行，审批通过后由下一轮查询体现 |
| MQ 实投 | 默认 NullPublisher；开启 MQ 后注入真实 publisher |
| 前端 confirm 交互 | SSE `confirm` 事件已发；客户端需带 `confirm_token` 的下一轮请求 |

## 4. 常用命令

```powershell
.venv\Scripts\python.exe -m pytest -q
.venv\Scripts\python.exe -m pytest -m mysql -q   # 需 MYSQL_TESTS_URL
.venv\Scripts\python.exe -m app.eval.cli run --offline --no-fail-on-gate
```

## 5. 最近提交

| 提交 | 内容 |
| --- | --- |
| `39111a5` | 状态机/幂等/双确认/工具/审批/API 第一刀 |
| `bae281f` | 图接入 + 写工具审计 + outbox |
| `d4789de` | MySQL 用例 + CI job |
| `1422391` | CI 失败 annotation |
| `9831ed1` | 修嵌套事务，CI 7/7 |
