# M2 工单状态

> 状态：TODO / DOING / DONE / BLOCKED。每轮结束同步一次；验证记录见 `03-verification.md`。

| ID | 内容 | 依赖 | 状态 | 备注 |
| --- | --- | --- | --- | --- |
| M2-00 | 需求分析 + 方案 + 工单 | — | **DONE** | `01-plan.md`（目标/决策/风险）+ 本表 |
| M2-01 | 状态机写库服务（唯一入口/乐观锁/流水/outbox） | M2-00 | **DOING** | `services/transition.py` + `aftersales.py` 已实现；MySQL 并发测试待 CI job |
| M2-02 | 幂等层（Redis NX + 指纹 + DB 唯一键） | M2-01 | **DOING** | Redis 存储 + 指纹已实现并单测；DB `uk_idem` 并发测试待 CI |
| M2-03 | 确认令牌（签发/单次/过期/绑定） | M2-02 | **DONE** | 单次消费/换参/换人/Redis 故障失败关闭，6 例单测 |
| M2-04 | 只读工具（订单/物流/退款/库存 + 超时降级 + 审计） | M2-01 | **DONE** | Mock Provider + 超时降级 + buyer 越权 403；审计表接线待 M2-09 |
| M2-05 | 写工具（create/cancel + confirm + 幂等 + 事务） | M2-02/03 | **DONE** | `AfterSalesTools` + `tool_audit` 写审计；token 单次消费后进状态机 |
| M2-06 | 审批（>500 建任务、HMAC 回调、幂等、推进、outbox） | M2-05 | **DOING** | 服务 + HMAC 验签 + 幂等分支已实现；DB 集成待 CI |
| M2-07 | API 接线（apply/detail/cancel/approval/orders） | M2-06 | **DOING** | 五个端点已接线；错误码/鉴权 7 例接口测试通过 |
| M2-08 | 图接入（after_sales 意图 → 双确认 → 状态机 → 挂起/恢复） | M2-07 | **DONE** | `intake`/`query_status` 节点；两轮 confirm（token 落 session）；pending_action 跨轮恢复；3 例图测试 |
| M2-09 | outbox dispatcher + 消费者幂等 | M2-06 | **DONE** | 轮询投递 + 指数退避 + 死信；消费者 Redis NX 去重 + 标记 consumed |
| M2-10 | 评测/回归 + CI MySQL 办事链路 job | M2-09 | **DOING** | 并发幂等/迁移/版本冲突/回调幂等/拒绝恢复 5 例 + `aftersales-mysql` job 已加；待 CI 实跑验证 |
| M2-11 | 文档收尾（接口示例/runbook/交接） | M2-10 | TODO | 最后一轮 |
