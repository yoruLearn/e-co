# M3 工单状态

| ID | 内容 | 状态 | 说明 |
| --- | --- | --- | --- |
| M3-01 | SLO 规则 + 告警 | **DONE** | `deploy/prometheus/rules-slo.yml`（可用性/延迟/错误预算/无引用/死信/写 503） |
| M3-02 | staging 拓扑叠加 | **DONE** | `deploy/docker-compose.staging.yml`（2 副本/资源限制/健康检查/28d 保留） |
| M3-03 | SSE 压测脚本 | **DONE** | `scripts/loadtest.py`（首片段/端到端 P95、错误率、token） |
| M3-04 | 备份/恢复脚本 | **DONE** | `scripts/backup.ps1`、`scripts/restore.ps1`（SHA256 + 表数校验） |
| M3-05 | 上线/演练/安全清单 | **DONE** | `docs/m4/01-go-live-checklist.md`、`02-security-review.md`、`03-drills.md` |
| M3-06 | 指标看板 | **DONE** | 复用 M1 Grafana 看板（延迟/首片段/5xx/缓存/无引用） |
| M3-07 | 并发压测（50/100/200） | **BLOCKED** | 无 staging/本机无 Docker；脚本就绪，待执行 |
| M3-08 | 回滚演练 ≤15min | **BLOCKED** | 需要上一版本镜像与发布环境 |
| M3-09 | 备份恢复演练（RPO/RTO） | **BLOCKED** | 需要真实数据量与可重建实例 |
| M3-10 | 告警触达 fire drill | **BLOCKED** | 需要真实 alertmanager receiver/值班 |
| M3-11 | 灰度 5→20→100 实测 | **BLOCKED** | 需要网关/多副本环境 |
| M3-12 | 依赖故障演练 | **BLOCKED** | 需要 staging 拓扑 |
