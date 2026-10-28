# M3 生产化 · 方案与工单

> 依据 Spec §15（SLO）、§17（部署/灰度/回滚/备份）、§19（告警）。
> **M3 分两类**：可在仓库内交付并验证的（告警/脚本/拓扑/清单）标 DONE；必须在 staging 或生产执行的（压测、回滚演练、告警触达）标 BLOCKED，不伪造。

## 1. 出口标准（Spec §15 / §17）

| 维度 | 门禁 | 状态 |
| --- | --- | --- |
| SLO | 可用性 99.9%（28d）、对话 P95 ≤1.5s（缓存命中）、写链路错误率 ≤0.5% | 指标与告警已定义；**staging 压测待执行** |
| 回滚 | 版本回滚 ≤15 分钟（实测） | 流程+脚本已写；**演练待执行** |
| 备份 | MySQL 每日全备 + binlog 7 天；恢复演练 RPO≤15min/RTO≤1h | 备份/恢复脚本已交付；**恢复演练待执行** |
| 告警 | P0/P1 告警 5 分钟内触达值班 | 规则已写；**触达需接 alertmanager/IM** |
| 灰度 | 5%→20%→100% 可控放量、可回退 | 流程已写；**开关需 staging 验证** |

## 2. 本轮交付（可验证）

| # | 内容 | 落点 |
| --- | --- | --- |
| M3-01 | SLO 记录规则 + 错误预算/业务红线告警 | `deploy/prometheus/rules-slo.yml` |
| M3-02 | staging 叠加拓扑（副本/资源/保留/健康检查） | `deploy/docker-compose.staging.yml` |
| M3-03 | 压测脚本（SSE 并发、首片段/端到端 P95、错误率） | `scripts/loadtest.py` |
| M3-04 | 备份/恢复脚本（MySQL + Redis + 校验和） | `scripts/backup.ps1`、`scripts/restore.ps1` |
| M3-05 | 上线/演练/安全清单 | `docs/m4/*.md` |
| M3-06 | 指标与看板（M1 已交付，M3 复用） | `deploy/grafana/provisioning/dashboards/m1-rag.json` |

## 3. 需要在 staging/生产执行（BLOCKED，未完成）

| # | 事项 | 为什么不能在这里完成 | 执行方式 |
| --- | --- | --- | --- |
| M3-07 | 50/100/200 并发压测，出 P95/错误率/成本曲线 | 本机无 Docker/MySQL/Chroma（Hub 不可达） | `python scripts/loadtest.py --concurrency 50 ...` |
| M3-08 | 回滚演练（镜像回退 + DB 迁移兼容）并计时 | 需要可发布环境与上一版本镜像 | 按 `docs/m4/03-drills.md` 执行 |
| M3-09 | 备份恢复演练（RPO/RTO 实测） | 需要真实 MySQL 数据量 | `backup.ps1` → 新库 `restore.ps1` → 校验行数 |
| M3-10 | 告警触达（IM/电话）与值班表 | 需要真实 webhook/值班系统 | 配置 alertmanager receiver 后做一次 fire drill |
| M3-11 | 灰度 5%→20%→100% 开关实测 | 需要网关/多副本环境 | 按 `docs/m4/03-drills.md` |
| M3-12 | 依赖故障演练（Redis/Chroma/模型超时） | 需要 staging 拓扑 | 按 runbook 逐项注入 |

## 4. 风险

| 风险 | 应对 |
| --- | --- |
| 没有 staging 就宣称 SLO 达标 | 本文明确区分"已定义"与"已实测"；M4 签字前必须补实测 |
| 回滚依赖 DB 迁移不可逆 | 迁移只做加法（新增表/列），回滚只回镜像不回 schema |
| 备份从未恢复过 | 恢复脚本带 SHA256 校验 + 表数量断言；首次演练必须人工核对业务数据 |
