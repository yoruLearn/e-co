# 运行手册（Runbook）· M0

> 对齐 Spec §19.1 告警表。M0 只有骨架，能执行的处置已给出可直接复制的命令；
> 依赖业务链路的处置标注了归属里程碑，**不写无法验证的步骤**。

## 0. 通用排障入口

```powershell
pwsh scripts/status.ps1                     # 容器状态 + /healthz + /readyz 明细
docker compose -f deploy/docker-compose.yml logs --tail 100 api
docker compose -f deploy/docker-compose.yml logs --tail 100 worker
curl.exe -s localhost:8000/readyz | ConvertFrom-Json | ConvertTo-Json -Depth 6
curl.exe -s localhost:8000/metrics | Select-String 'outbox_pending|dlq_total|transition_illegal'
```

日志是 JSON line，取一次请求的完整链路：

```powershell
docker compose -f deploy/docker-compose.yml logs api | Select-String 'tr_8f3c'
```

---

## 1. api 不可用（P1）

**判据**：`up{job="aftersales-api"} == 0`、`/healthz` 超时或 5xx。

1. `docker compose ps` 看容器状态；`Restarting` 说明进程反复退出。
2. `docker compose logs --tail 200 api` 找 `app.startup` / `Traceback`。
3. 常见原因与处置：
   - 配置非法（如 prod 用默认 JWT 密钥）→ 启动即失败，日志有 `禁止使用默认开发 JWT 密钥`；修正 `deploy/.env` 后重启。
   - 端口占用 → 改 `API_PORT`。
   - 依赖未就绪 → 这不是 api 挂，转第 2 节。
4. 单副本故障（生产）：编排层摘流后重建该副本；双副本下不影响服务。

## 2. `/readyz` 503（P2）

`readyz` 返回 `detail.checks[]`，按失败项处置：

| 失败项 | 排查 | 处置 |
| --- | --- | --- |
| `mysql` | `docker compose logs mysql`；`mysqladmin ping` | 等待启动（healthcheck 12×10s）或扩连接数 |
| `migrations` | 明细含 `数据库版本 x 落后于脚本 head y` | `docker compose exec api alembic upgrade head` |
| `redis` | `docker compose exec redis redis-cli ping` | 重启 redis；缓存全丢可接受（Spec §17.1） |
| `mq` | `status: skipped` 属正常（`MQ_ENABLED=false`）；`fail` 才处理 | M2 接入后按 §12.3 排查 |
| `chroma` | `curl chroma:8000/api/v1/heartbeat` | 重启 chroma；索引可重建（§17.4） |

> 注意：`skipped` **不等于**健康。它表示该依赖在当前配置下未启用。

## 3. 状态机非法迁移（P1）

**判据**：`transition_illegal_total > 0`（M2 起才会有数据）。

1. 立即冻结写链路：`POST /api/v1/admin/breaker {"scope":"write","open":true}`（M3 落地，M0 返回 501）。
2. 按 `trace_id` 取证：`GET /api/v1/traces/{trace_id}`（M1 落地）+ `aftersales_transition` 流水。
3. 数据订正必须走人工脚本并留审计；禁止直接 `UPDATE ... SET status=`。
4. 24h 内出故障报告（时间线/根因/改进项，Spec §19.2）。

## 4. 延迟恶化（P2）

**判据**：核心接口 P95 恶化 > 30% 持续 10min。

1. 看 `/metrics`：`http_request_duration_seconds` 按 path 分布。
2. M1 起追加：`rag_cache_hit_ratio`（缓存是否失效）、`llm_tokens_total`（是否重试打爆）。
3. 处置：确认是否有索引重建/缓存击穿；必要时降级（M3 熔断开关）。

## 5. outbox 积压（P2）

**判据**：`outbox_pending > 1000` 持续 5min（M2 起有数据）。

1. 查 MQ broker 健康与消费组堆积；
2. 扩 worker 副本（无状态，`docker compose up -d --scale worker=3`）；
3. 检查首批死信：`SELECT * FROM outbox_message WHERE status='dead' ORDER BY id DESC LIMIT 20;`。

## 6. 死信增长（P2）

1. 按 `msg_id` 定位业务单据，评估是否已对用户承诺；
2. 修复下游后重投：把 `status` 置 `pending`、`retry=0`、`next_retry_at=now()`；
3. 记录到故障报告，避免同类消息重复进死信。

## 7. 写链路熔断（P2）

**判据**：`write_error_ratio > 0.5`（60s 窗口）。

1. 确认降级话术已生效（用户侧应看到"转人工"而非报错）；
2. 排查错误码分布：`AS-5001`（内部）优先看下游超时；`AS-5031` 为熔断本身；
3. 半开恢复：M3 提供 `/admin/breaker` 手动复位，或等待 30s 自动半开探测。

## 8. 发布与回滚（M3 完善，M0 已定流程）

| 步骤 | 命令/接口 | 说明 |
| --- | --- | --- |
| 迁移先行 | `alembic upgrade head` | 只允许 expand，旧代码需能继续跑 |
| 滚动替换 | `docker compose up -d --no-deps api` | `/readyz` 通过才接流量 |
| 回滚镜像 | 切回上一 digest | 目标 ≤ 15min（含观察） |
| 回滚 Prompt | `POST /admin/prompts/{key}/publish {"version":N-1}` | M3 |
| 回滚索引 | `POST /admin/index/{ver}/activate` | 旧版自动 deprecated，缓存随版本失效 |

## 9. 备份恢复（M3 演练）

M0 提供能力清单，脚本在 M3 交付：

- MySQL：每日全备 + binlog 归档，RPO ≤ 5min / RTO ≤ 30min；
- Redis：AOF everysec，缓存丢光可按 §17.1 预热；
- Chroma：每日快照 + 源文档全量重建（权威源是文档 + `index_version` 表）。
