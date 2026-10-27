# M0 验证记录 · 电商售后管家

| 项 | 内容 |
| --- | --- |
| 验证日期 | 2026-10-27 |
| 环境 | Windows 11 / Python 3.11.15（uv 管理）/ Docker CLI 29.8（**本机 daemon 未启动**） |
| 远端 | GitHub Actions：<https://github.com/yoruLearn/e-co/actions> —— **run #3 全绿（4/4 jobs）** |
| 结论 | 本地 11 项门禁 + 远端 4 个 CI job **全部通过**；真实 MySQL 8 迁移与镜像构建/trivy 扫描已在 CI 实测 |

---

## 1. 门禁结果

| # | 命令 | 结果 |
| --- | --- | --- |
| 1 | `ruff check .` | ✅ All checks passed（82 个文件） |
| 2 | `ruff format --check .` | ✅ 82 files already formatted |
| 3 | `mypy app`（strict） | ✅ Success: no issues found in 55 source files |
| 4 | `pytest -q --cov=app` | ✅ **280 passed, 1 skipped**，覆盖率 **89.46%**（门禁 80%） |
| 5 | `docker compose -f deploy/docker-compose.yml --env-file deploy/.env.example config -q` | ✅ exit 0 |
| 6 | `python scripts/smoke.py` | ✅ `/healthz` 200、`/readyz` 503（逐项明细 5 项）、`/metrics` 200、trace 头透传 |
| 7 | `python -m app.workers.runner --once` | ✅ exit 0（Redis 不可达时只告警，不崩溃） |
| 8 | `alembic upgrade head --sql` | ✅ exit 0，生成 `alembic_version` + **9 张业务表**的 DDL |
| 9 | `python -c "import app.main"` | ✅ 导入成功，13 条路由（含探针与 `/metrics` 挂载） |
| 10 | `powershell -File scripts/ci.ps1`（Windows PowerShell **5.1**） | ✅ `ALL GATES GREEN`（5 段门禁一次跑完） |
| 11 | `powershell -File scripts/up.ps1`（daemon 未启动） | ✅ 直接报错退出（exit 1）并给出「先启动 Docker Desktop」提示，不产生误导性堆栈 |

冒烟实际输出（依赖未起，验证降级路径）：

```text
[smoke] /healthz -> 200 code=0
[smoke] /readyz -> 503 checks=['mysql', 'migrations', 'redis', 'mq', 'chroma']
[smoke] /metrics -> 200
[smoke] OK
```

worker 心跳失败时的日志（证明"告警但不崩"）：

```json
{"level":"WARNING","logger":"app.worker","msg":"worker.heartbeat.failed",
 "reason":"TimeoutError","key":"worker:heartbeat:<host>"}
{"level":"INFO","logger":"app.worker","msg":"worker.tick","ok":false}
{"level":"INFO","logger":"app.worker","msg":"worker.stopped","beats":1}
```

## 2. 覆盖率明细（关键模块）

| 模块 | 覆盖率 | 说明 |
| --- | --- | --- |
| `services/state_machine.py` | 97% | §5.2 迁移表全量参数化 |
| `services/outbox.py` | 100% | 信封/退避/死信阈值 |
| `services/idempotency.py` | 100% | key 规则与指纹 |
| `services/approval.py` | 100% | 阈值严格大于语义 |
| `app/rag/retriever.py` | 100% | 三档阈值 + RRF |
| `rag/cache.py` | 98% | 键规范与 TTL |
| `tools/registry.py` | 98% | 7 工具/分级/超时 |
| `api/*` 路由 | 91–100% | 信封、鉴权、501、SSE |
| `common/config.py` | 95% | 校验规则与密钥读取 |
| `common/logging.py` | 88% | 脱敏/保留字转义 |
| `services/health.py` | 77% | 真实依赖探测分支无法在无依赖环境覆盖（已由假 checker 覆盖聚合逻辑） |

## 3. 交付物清单

```text
app/{api,agent,rag,tools,services,workers,eval,db,common,migrations}   55 个源文件
tests/{unit,integration,e2e}                                          18 个测试文件 / 280 用例
deploy/  Dockerfile · compose(9 服务+4 profile 服务) · nginx · prometheus(+alerts) · grafana · loki · alertmanager · .env.example
scripts/ up.ps1 · down.ps1 · status.ps1 · ci.ps1 · smoke.py
docs/    m0/01-需求分析 · m0/02-tickets · m0/03-验证(本文) · adr/0001 · runbook
evals/cases/  四类用例 11 条（typical/multi_hop/no_answer/multi_turn）
.github/workflows/ci.yml  四 job：quality / smoke / compose+trivy / migration
```

## 4. 未覆盖项与原因（诚实清单）

| 项 | 状态 | 原因 / 后续 |
| --- | --- | --- |
| Docker 栈整体起停（mysql/redis/chroma + api/worker + nginx 同时在线） | ⚠️ 本机未实测 | 本机 Docker Desktop daemon 未运行（`up.ps1` 的 daemon 守卫已验证会明确报错）；CI 已实测「compose config + 镜像构建 + trivy + 真实 MySQL 迁移」，缺的只是多容器同栈联跑（M1 接 RAG 时补） |
| 镜像构建与 trivy 扫描 | ✅ 已实测 | CI `compose` job：build 成功，trivy CRITICAL=0（ignore-unfixed） |
| RocketMQ 协议接入 | ⛔ 计划 M2 | M0 仅做 NameServer TCP 探活，避免为无流量能力引入重依赖 |
| 真实登录 / 用户中心 | ⛔ 计划 M4 | Spec 未定义用户源，不臆造；staging/prod 显式返回 501 |
| `alembic check`（模型漂移自动检测） | ⏭ 计划 M1 | 先用"表集合三向比对"单测兜底；`alembic check` 需真实库，接入 CI 更合适 |
| Prometheus 多进程模式 | ⏭ 计划 M3 | 当前 1 进程/副本，双副本由 Prometheus 分别抓取；若改 `--workers > 1` 需 multiprocess 目录 |
| `/metrics` 访问控制 | ⏭ 计划 M3 | 仅内网暴露（compose 未对公网映射）；生产由 LB/白名单限制 |
| Windows PowerShell 7 语法实测 | ⏭ 计划 M3 | 本机仅有 PowerShell 5.1；脚本已按 5.1/7 双兼容编写（ASCII 输出、无 PS7 专有 cmdlet） |

## 5. M0 验收对照（Spec §20「CI 绿灯，一键起停」）

| 验收点 | 证据 |
| --- | --- |
| CI 绿灯 | 本地 11 项门禁全绿（§1）；**GitHub Actions run #3 4/4 job 全绿**（§6） |
| 一键起停 | `scripts/up.ps1`（daemon 检查 → 依赖健康 → 迁移 → 探针轮询 → 打印入口）/ `down.ps1`；`compose config` 校验通过 |
| 工程脚手架 | 目录与 Spec §16.1 一致，55 个源文件，strict mypy |
| compose | 9 个常驻服务 + mq/obs profile，健康检查与启动顺序齐备 |
| trace / 统一异常 | 每响应带 `X-Trace-Id`；12 个错误码统一信封；未捕获异常不泄漏堆栈 |
| `/healthz` + `/readyz` | 探针单测 + 进程冒烟 + 容器 HEALTHCHECK 三处落地 |

---

## 6. CI 远端实跑记录（GitHub Actions）

仓库：<https://github.com/yoruLearn/e-co>（public，默认分支 `main`，workflow：`.github/workflows/ci.yml`）

| run | 触发动作 | 结果 | 关键信息 |
| --- | --- | --- | --- |
| #1 | 首次推送 | ❌ 2/4 job | quality / smoke 绿；compose+trivy、migration 失败 |
| #2 | 修依赖 + trivy tag | ❌ 3/4 job | migration 转绿（真实 MySQL 8 upgrade/9 表/downgrade 全过）；镜像构建失败 |
| #3 | 修 `.dockerignore` | ✅ **4/4 job** | quality / smoke / compose+trivy / migration 全绿 |

### 6.1 首轮实跑抓到的真实缺陷（本地门禁抓不到的那一类）

| # | 缺陷 | 为什么本地没发现 | 修复 |
| --- | --- | --- | --- |
| 1 | 缺 `cryptography` 依赖 | `aiomysql` 连 MySQL 8 默认认证 `caching_sha2_password` 时会抛 `cryptography is required ...`；本机无 MySQL，纯单测不会触发。同一依赖也是 PyJWT **RS256** 的签名后端（生产形态必用） | `pyproject.toml` 增加 `cryptography>=43,<47`；补 RS256 密钥对往返单测做回归守卫；`uv.lock` 重生成 |
| 2 | `.dockerignore` 排除了 `README.md` | `pip install .` 需要 `pyproject.toml` 的 `readme = "README.md"`，Dockerfile 也 `COPY README.md`；镜像构建 7s 内失败 | `.dockerignore` 增加 `!README.md` 例外 |
| 3 | trivy action tag 不存在（`0.28.0`） | 本地不跑 GitHub Actions，`uses:` 解析失败只在远端 "Set up job" 暴露 | 改为有效 tag `aquasecurity/trivy-action@v0.36.0` |

> 结论：这三次失败恰好证明 M0 门禁分层是对的——静态检查与单测**无法替代"在真实依赖上跑一遍"**，
> 因此 `compose`（镜像构建 + trivy）与 `migration`（真实 MySQL）两个 job 不是装饰，而是 M0 出口的必要条件。

---

## 7. 下一步迭代方案（M1 · 问答链路）

**目标（出口标准）**：典型用例完成率 ≥ 85%、引用正确率 ≥ 95%、首 token P95 ≤ 800ms、端到端 P95 ≤ 1.5s
（Spec §13.3 / §15 / §20 M1）。

### 7.1 迭代范围

| ID | 任务 | 依据 | 交付物 | 验收 |
| --- | --- | --- | --- | --- |
| M1-01 | 语料接入与索引管道 | §7.1 | `rag/chunker.py` 实现（500/80、标题层级）、`indexer.py`（批量 32 向量化 + Chroma collection `aftersales_v{N}`）、`index_version` 表状态流转 | 建索引成功、`doc_count` 正确、`building→active` 可切换 |
| M1-02 | 混合召回 + 重排 | §7.2 | Chroma Top20 + MySQL `MATCH` Top20 → RRF（已实现）→ Reranker Top5，分数归一化 0–1 | RRF 融合结果与手算一致；Top5 召回率基线记录 |
| M1-03 | 阈值兜底与引用作答 | §7.2 / G2 | `classify_with_settings` 接线；`citations[]` chunk 级出处；Top1 < 0.55 禁止生成 | no_answer 用例 100% 兜底、零编造；引用正确率 ≥ 95% |
| M1-04 | LangGraph 图装配 + SSE 流式 | §6.1 / §6.2 / FR-CHAT-06 | `router/rewrite/retrieve/answer/clarify/reject/sse_out` 节点、条件边、`RedisSaver`、`delta/tool/citation/done` 事件 | 首 token P95 ≤ 800ms；图重放（checkpoint）可用 |
| M1-05 | 会话记忆与三级缓存 | §7.3 / §11 | 滑动窗口 + 超 10 轮摘要；`rag:`/`lc:`/`faq:` 缓存 + 双检锁防击穿 + 索引版本联动失效 | 缓存命中率与 Embedding 调用量下降 ≥ 60% |
| M1-06 | Prompt Registry | §6.3 | 表 + 加载缓存 + `key@version` + 注入防御包裹 `<user_input>` + 出口脱敏 | 发布/回滚可切版本；注入红队用例通过 |
| M1-07 | `/eval/run` 与 90 条用例 | §13.3 | 断言执行器、四类用例（40/15/15/20）、失败归因、`eval_run` 落库、门禁 | 完成率/引用率/P95 三指标输出；门禁可阻断 |
| M1-08 | trace 调用树 | §13.1 / FR-OBS-01 | `GET /traces/{id}` 聚合 api→node→tool→rag→mq→db 各段耗时与 token | 单 trace_id 还原完整调用树 |
| M1-09 | 指标接线 | §13.2 | `chat_latency_seconds`、`rag_cache_hit_ratio`、`llm_tokens_total` 真实上报 + Grafana 看板 v1 | 看板可见四类核心曲线 |
| M1-10 | 质量门禁与基线 | §16.3 | CI 增加评测抽样（可 mock LLM）、`pytest` 覆盖 RAG 关键路径 ≥ 85% | CI 绿灯 + 基线归档 |

### 7.2 技术方案要点

1. **Embedding/LLM 选型未定（Spec Q2）**：M1 先用可配置 provider + `LLM_MOCK` 录制回放跑通链路，staging 再对比选型，避免被供应商阻塞。
2. **评测先行**：先补 90 条用例（含 20 条多轮、15 条无答案），再写图 —— 否则"完成率"无法验收。
3. **引用约束在生成层与校验层双保险**：Prompt 要求逐句绑定 `citation_id`，输出后置校验无引用即降级兜底（G2）。
4. **缓存与索引版本强绑定**：`rag:{ver}:{hash}`，索引激活时按前缀 DEL，杜绝旧索引结果（FR-CACHE-03）。
5. **成本控制前置**：节点级调用上限（router/rewrite/answer 各 3 次）+ 上下文裁剪（tool_results 5 / citations 3）在 M1 就接线，不留到 M3。

### 7.3 M1 风险与应对

| 风险 | 应对 |
| --- | --- |
| 无真实语料导致召回效果假象 | 先用售后政策/FAQ 文档建 3 个版本索引，评测集覆盖四类；指标不达标不进入 M2 |
| LLM 不稳定导致评测抖动 | 录制回放（VCR）跑 CI 回归，真实模型只在 staging 跑；温度 0 + 结构化输出 |
| SSE 与 LangGraph 流式适配复杂 | 先在 M0 的 SSE 骨架上接假 token 流做契约测试，再接真图 |
| 检索缓存命中率低 | 上线前用压测脚本量化 P95 与命中率，决定是否加本地 Caffeine 层 |

### 7.4 建议节奏

| 周 | 内容 |
| --- | --- |
| 第 1 周 | M1-01/02/03（索引 → 召回 → 引用作答，离线可测） |
| 第 2 周 | M1-04/05/06（图 + SSE + 缓存 + Prompt） |
| 第 3 周 | M1-07/08/09/10（评测、trace、看板、门禁）+ M1 出口评审 |

> M2（办事链路）依赖 M1 的图与工具契约，但**不阻塞**：`state_machine`/`idempotency`/`outbox` 的纯函数层已在 M0 完成，
> M2 可直接进入"接 DB + Redis + MQ"的实现阶段。
