# M0 Tickets · 电商售后管家骨架

> 由 `01-requirements-analysis.md` 派生。执行顺序即编号顺序；「依赖」列只列硬依赖。
> 状态：`TODO` / `DOING` / `DONE`。验收命令统一用 `uv run ...`（Windows 亦可用 `.venv\Scripts\python -m ...`）。

| ID | 标题 | 依据 | 依赖 | 交付物 | 验收判定 | 状态 |
| --- | --- | --- | --- | --- | --- | --- |
| M0-01 | 工程骨架与依赖锁定 | §16.1 §16.2 §20 | — | `pyproject.toml`、`.python-version`、目录树、`ruff/mypy/pytest/coverage` 配置 | `uv sync --extra dev` 成功；`python -c "import app.main"` 通过；9 个 `app/*` 子包存在 | DONE |
| M0-02 | 配置基座 pydantic-settings | §16.2 §17.5 | M0-01 | `app/common/config.py`、`deploy/.env.example` | 字段与 `.env.example` 一一对应（单测）；prod 默认密钥 fail-fast；`*_FILE/*_PATH` 密钥读取 | DONE |
| M0-03 | 结构化日志 | §13.1 §16.2 | M0-02 | `app/common/logging.py` | 输出为合法 JSON line；含 `ts/level/logger/msg/trace_id`；`log_event` 附加字段可解析；敏感键打码 | DONE |
| M0-04 | trace 上下文与中间件 | §13.1 §9.1 | M0-03 | `app/common/trace.py`、`app/api/middleware.py` | 响应头回写 `X-Trace-Id`；非法头被替换；日志 `trace_id` 一致；contextvar 无泄漏 | DONE |
| M0-05 | 统一错误码与异常处理 | §9.1 | M0-04 | `app/api/errors.py`、`app/api/handlers.py`、`app/api/responses.py` | 9 个 Spec 错误码 HTTP 映射单测；校验错误→AS-4001；未捕获→AS-5001 且无堆栈泄漏 | DONE |
| M0-06 | 指标与 `/metrics` | §13.2 | M0-04 | `app/common/metrics.py`、指标中间件 | `/metrics` 暴露 `http_requests_total`、`http_request_duration_seconds`、`app_info`；§13.2 指标名登记 | DONE |
| M0-07 | 健康探针 | §9.2 §17.6 | M0-02 M0-05 | `app/services/health.py`、`app/api/routers/health.py` | `/healthz` 200 且不访问外部；`/readyz` 并发检查 mysql/redis/mq/chroma/migrations，缺依赖 503 + 明细；总耗时受 `READY_TIMEOUT_MS` 约束 | DONE |
| M0-08 | 客户端与依赖注入 | §2.2 §11 §12 | M0-02 | `app/common/clients.py`、`app/api/deps.py` | engine/redis/httpx 懒连接单例，lifespan 关闭；依赖未就绪不阻塞启动 | DONE |
| M0-09 | JWT 鉴权依赖 | §9.1 §14 | M0-08 | `app/common/security.py`、`app/api/deps.py`、`routers/auth.py` | 无/坏/过期 token→AS-4010；角色不足→AS-4030；dev 登录签发、prod 501 | DONE |
| M0-10 | API 路由骨架与契约模型 | §9.2 | M0-05 M0-09 | `app/api/routers/*.py`、`app/api/schemas.py`、`app/api/sse.py` | `/openapi.json` 含 §9.2 全部路径；未实现端点→AS-5002；SSE 端点 `text/event-stream` + `error/done` 骨架 | DONE |
| M0-11 | 数据层与 Alembic 初始迁移 | §10.1 §17.3 | M0-02 | `app/db/{session,models}.py`、`app/migrations/**` | 迁移覆盖 9 张表；ORM 表名集合==迁移表名集合（单测）；`alembic upgrade head` 可执行 | DONE |
| M0-12 | 纯函数内核（状态机/工具/阈值/缓存键/outbox） | §5.2 §8.1 §7.2 §7.3 §12.2 | M0-01 | `app/services/state_machine.py`、`app/tools/registry.py`、`app/rag/**`、`app/services/{outbox,idempotency,approval}.py` | §5.2 全表合法迁移通过 + 全非法迁移抛 AS-4003；工具 7 个（5 ro/2 write）；阈值三档边界；缓存键规范；信封字段校验 | DONE |
| M0-13 | worker runner | §17.6 | M0-08 | `app/workers/runner.py` | `python -m app.workers.runner --once` 退出码 0；心跳写 Redis（不可用则告警日志不崩溃）；SIGTERM 优雅退出 | DONE |
| M0-14 | Compose/Dockerfile/nginx/可观测栈 | §17.6 §19.1 §2.3 | M0-07 M0-13 | `deploy/**` | `docker compose config -q` 通过；api/worker 同镜像双入口；profile 隔离 mq/obs；prometheus 抓取 + 告警规则 | DONE |
| M0-15 | 一键起停脚本 | §20 | M0-14 | `scripts/up.ps1`、`down.ps1`、`status.ps1`、`ci.ps1`、`smoke.py`、`Makefile` | `up.ps1` 起栈并轮询 `/readyz`；`down.ps1 -Volumes` 清卷；`status.ps1` 打印健康矩阵 | DONE |
| M0-16 | 测试基座与用例 | §16.3 | M0-01…M0-12 | `tests/conftest.py`、`tests/unit/**`、`tests/integration/**`、`tests/e2e/**` | `pytest -q` 全绿；核心覆盖 ≥ 80%（`fail_under`）；integration 默认不依赖外部组件 | DONE |
| M0-17 | CI 流水线 | §16.3 | M0-16 | `.github/workflows/ci.yml` | lint/type/test/coverage/compose-config/migration/smoke/trivy 多段；与本地命令一致 | DONE |
| M0-18 | 文档与 ADR | §16.2 §19 §20 | 全部 | `README.md`、`docs/adr/0001-*`、`docs/runbook.md`、`docs/m0/*` | README 从零到 ready ≤15min；ADR 记录 6 个关键决策；Runbook 对齐 §19.1 告警 | DONE |
| M0-19 | 全量验证与收尾 | §20 | 全部 | `docs/m0/03-verification.md` | ruff/mypy/pytest/compose/smoke 全绿并留痕 | DONE |
