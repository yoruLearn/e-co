# M2 办事链路 · 实施方案

> 依据：`电商售后管家-系统开发Spec.md` §3.2/§3.3（FR-AS / FR-HITL）、§4.2 时序、§5 状态机、§8 工具层、§9.2 接口、§10.2 事务边界、§12 outbox。
> 前置：M1 已完成（问答链路、SSE、会话、缓存、评测、trace、限流、CI 6 job 全绿）。

---

## 1. 目标与出口标准

**一句话目标**：让"我要退这单"真正能办——工具取参 → 双确认 → 状态机受理 → 超阈值进审批 → 回调推进 → outbox 事件，全程可幂等、可恢复、可审计。

| 维度 | 门禁 | 测量方式 |
| --- | --- | --- |
| 幂等 | 并发 10 次重复提交只产生 1 张单据 | MySQL 集成测试（`uk_idem` + Redis SET NX） |
| 迁移合法性 | 迁移表全部非法用例被拒绝且返回 `AS-4003` | 表驱动单测 + 接口测试 |
| 双确认 | 未确认不调用写工具；token 单次、5 分钟 | 单测 + 接口测试 |
| 审批 | 金额 > 500 元进审批；回调幂等；重复回调结果一致 | 服务/接口测试 |
| 恢复 | 审批挂起后进程重启可继续（checkpoint + DB 状态） | 集成测试（memory checkpointer 复跑） |
| outbox | 业务与消息同事务；dispatcher 至少一次投递 | 事务测试 + dispatcher 单测 |
| 回归 | M1 全部门禁不回退 | CI 7 job（新增 MySQL 办事链路 job） |

---

## 2. 关键决策

| # | 决策 | 理由 |
| --- | --- | --- |
| D1 | **biz + transition + approval_task + outbox 同一事务** | Spec §10.2：事务内只写 outbox，不发 MQ；避免"单子建了消息丢了" |
| D2 | 幂等 = 客户端 `Idempotency-Key`（Redis SET NX 24h）+ DB `uk_idem` 双保险 | Redis 丢数据时 DB 唯一键仍兜底；参数指纹不一致 → `AS-4004` |
| D3 | `confirm_token` 由 `confirm` 事件签发，Redis 单次消费、TTL 300s，绑定 `user_id + action + order_id + amount` | FR-HITL-01；换参数重放 token 必须失败 |
| D4 | `amount > 500`（严格大于）进审批；≤500 由系统自动通过（`actor=system`） | FR-HITL-02 默认阈值 500；避免小额也占审批队列 |
| D5 | 审批回调 HMAC-SHA256（`X-Signature`）覆盖**原始 body**，secret 走文件/env | 防重放/伪造；验签失败 401 |
| D6 | 只读工具走 Provider 接口；M2 用 `MockOrderProvider`（固定 fixtures），真实联调 M4 | 不阻塞主链路；Provider 边界即联调边界 |
| D7 | 状态只能经 `state_machine` 的唯一入口改；乐观锁 `UPDATE ... WHERE version=?` | Spec §5.2 约束 1/2；影响行数 0 → 409 重读 |
| D8 | 审批挂起用 LangGraph checkpoint + `PENDING_APPROVAL` 状态双写；恢复以 DB 为准 | 进程重启后不依赖内存图状态 |

---

## 3. 工单

| ID | 内容 | 依赖 | 验收 |
| --- | --- | --- | --- |
| M2-00 | 需求分析 + 本方案 + 工单表 | — | 文档评审通过 |
| M2-01 | 状态机写库服务：`transition()` 唯一入口、乐观锁、流水、outbox 同事务 | M2-00 | 表驱动迁移测试 + 并发版本冲突 |
| M2-02 | 幂等层：Redis `SET NX` + 参数指纹 + DB `uk_idem`；重放返回首次响应 | M2-01 | 并发 10 次 → 1 单；异参 → AS-4004 |
| M2-03 | 确认令牌：签发/单次消费/过期/绑定校验 | M2-02 | 未确认/过期/换参全部拒绝 |
| M2-04 | 只读工具层：订单/物流/退款进度/库存，超时 800/500ms，降级提示，tool_audit | M2-01 | 单测（超时、降级、越权 buyer 仅本人订单） |
| M2-05 | 写工具：`create_after_sales` / `cancel_after_sales`，前置 confirm + 幂等 + 事务 | M2-02/03 | 写工具调用审计齐全 |
| M2-06 | 审批：`approval_task` 创建（>500）、HMAC 回调、幂等回调、状态推进、outbox | M2-05 | 重复回调结果一致；错签 401 |
| M2-07 | API 接线：apply / detail / cancel / approval / orders | M2-06 | 契约 + 错误码测试 |
| M2-08 | 图接入：`after_sales` 意图 → tools_ro → confirm → state_machine → approval（挂起/恢复） | M2-07 | 多轮办单用例；重启后恢复 |
| M2-09 | outbox dispatcher + 消费者幂等（MQ 关闭时降级为本地消费） | M2-06 | 至少一次 + 重试/死信单测 |
| M2-10 | 评测/回归：幂等并发、非法迁移全表、回调幂等、恢复；CI 加 MySQL 办事链路 job | M2-09 | 新 job 全绿，M1 6 job 不退化 |
| M2-11 | 文档收尾：接口示例、runbook 增补、交接 | M2-10 | 文档与实现一致 |

---

## 4. 风险与开放问题

| 风险 | 影响 | 应对 |
| --- | --- | --- |
| 本机无 Docker（Hub 不可达） | 无法本地跑 MySQL 集成测试 | 单测覆盖纯逻辑；MySQL 集成放 CI job（service container） |
| 并发下 Redis 幂等键与 DB 唯一键时序 | 可能双写 | 先 DB 事务 + 唯一键兜底；Redis 只是快路径 |
| 审批回调重放 | 重复推进状态 | `callback_count` + 首次结果复用；状态机乐观锁 |
| 状态恢复依赖 checkpoint 版本 | 升级 LangGraph 可能读不回 | ADR-0002 已锁定版本；恢复以 DB 状态为准 |
| 真实订单 Provider 未联调 | 工具返回 mock | M4 硬条件；Provider 接口先冻结 |

---

## 5. 里程碑内顺序

```text
M2-00 → M2-01 → M2-02 → M2-03 → M2-04 → M2-05 → M2-06 → M2-07 → M2-08 → M2-09 → M2-10 → M2-11
```

每完成一个工单：本地门禁（ruff/mypy/pytest）→ 更新 `02-tickets.md` 与 `03-verification.md` → 提交 → CI 全绿。
