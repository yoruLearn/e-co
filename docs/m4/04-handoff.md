# M3/M4 交接：已交付 vs 必须由人完成

> 结论先说：**M3 的仓库内交付物已完成；M3 的验收类工作和 M4 的上线动作无法在当前环境完成**（无 staging/生产、无真实 Provider、无安全/运维签字权）。下方逐项列明，避免"代码完成=上线完成"的误判。

## 1. 已完成（仓库内可验证）

| 类别 | 交付物 |
| --- | --- |
| SLO/告警 | `deploy/prometheus/rules-slo.yml`（可用性/延迟/错误预算/无引用/死信/写 503） |
| staging 拓扑 | `deploy/docker-compose.staging.yml`（2 副本/资源/健康检查/28d 保留） |
| 压测 | `scripts/loadtest.py`（SSE 并发、首片段/端到端 P95、错误率、token） |
| 备份/恢复 | `scripts/backup.ps1`、`scripts/restore.ps1`（SHA256 + 表数校验） |
| 上线清单 | `docs/m4/01-go-live-checklist.md`（逐项状态 + 签字栏） |
| 安全评审 | `docs/m4/02-security-review.md` |
| 演练手册 | `docs/m4/03-drills.md`（回滚/灰度/备份恢复/故障注入） |
| 文档 | `docs/m3/01-plan.md`、`02-tickets.md`、`03-verification.md` |

本地验证：`ruff` / `mypy` / `pytest`（559 passed, 15 skipped）；YAML 可解析；`loadtest.py --help` 可用。

## 2. BLOCKED：必须由有环境的同学执行（我不能代签）

| 编号 | 事项 | 依赖 | 完成标志 |
| --- | --- | --- | --- |
| M3-07 | 50/100/200 并发压测 | staging | 回填 P95/错误率/成本到 `docs/m3/03-verification.md` |
| M3-08 | 回滚 ≤15min 演练 | 发布环境 + 上一镜像 | 记录耗时与失败点 |
| M3-09 | 备份恢复 RPO/RTO 演练 | 真实数据量 | 恢复后业务抽查通过 |
| M3-10 | 告警触达 fire drill | alertmanager/IM/值班 | P0 5 分钟内触达 |
| M3-11 | 灰度 5→20→100 | 网关/多副本 | 每阶段观察记录 |
| M3-12 | 依赖故障演练 | staging | 降级行为符合 runbook |
| M4-1 | 真实订单/物流/退款 Provider 联调 | 第三方接口与凭证 | 端到端办单成功 |
| M4-2 | 渗透测试与安全评审 | 安全团队 | 高危清零签字 |
| M4-3 | 密钥托管/轮换、备份加密 | 运维/DBA | 演练通过 |
| M4-4 | 上线放量与公告 | 产品/运营 | Go-Live 清单全勾选 |

## 3. 建议执行顺序

1. 起 staging（`docker compose -f deploy/docker-compose.yml -f deploy/docker-compose.staging.yml up -d`）；
2. 跑 `scripts/loadtest.py`（先 10 并发、再 50/100/200），把数字回填；
3. 按 `docs/m4/03-drills.md` 做回滚 + 备份恢复 + 故障注入；
4. 接 alertmanager receiver，做一次 P0 fire drill；
5. 安全评审关闭高危项；
6. Provider 联调通过后，按 5%→20%→100% 放量；
7. 全部勾选后签字，M4 才算完成。

## 4. 风险提示

- 本仓库的 M3/M4 文档是"可执行手册"，不是"已执行记录"；
- 任何未回填 `docs/m3/03-verification.md` 的项，都不得在 Go-Live 清单上打勾；
- SLO 数字只认 staging/生产实测，离线评测数字不能替代。
