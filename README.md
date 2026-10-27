# 电商售后管家 · aftersales-agent

[![ci](https://github.com/yoruLearn/e-co/actions/workflows/ci.yml/badge.svg)](https://github.com/yoruLearn/e-co/actions/workflows/ci.yml)

仓库：<https://github.com/yoruLearn/e-co>

对话式售后 Agent：把 **「问进度 → 答疑 → 发起退换货 → 审批 → 落库」** 收敛成一条对话流水线。
知识问题走 RAG 引用作答，实时数据走只读工具，写操作走状态机 + HITL 双确认 + outbox/MQ 异步落库。

> 当前里程碑：**M0 骨架**（工程脚手架 / compose / trace / 统一异常 / 健康探针）。
> 业务能力随 M1（问答链路）与 M2（办事链路）填充，未实现端点统一返回 `AS-5002`（501），
> 契约已按 Spec §9.2 固定，前端可先行联调。

| 文档 | 内容 |
| --- | --- |
| [需求分析（M0）](docs/m0/01-requirements-analysis.md) | 范围、验收映射、风险与偏差说明 |
| [Tickets（M0）](docs/m0/02-tickets.md) | 19 张工单、依赖与验收判定 |
| [验证记录（M0）](docs/m0/03-verification.md) | 实际执行结果与未覆盖项 |
| [ADR-0001](docs/adr/0001-m0-architecture-decisions.md) | 6 项关键决策（探针语义、错误码、表格即代码等） |
| [Runbook](docs/runbook.md) | 告警处置手册，对齐 Spec §19.1 |

---

## 1. 快速开始（本地进程）

要求：Python **3.11**、[uv](https://docs.astral.sh/uv/)。

```bash
uv sync --extra dev            # 安装依赖（生成/校验 uv.lock）
uv run uvicorn app.main:app --reload --port 8000
```

打开 <http://127.0.0.1:8000/docs>。此时：

- `GET /healthz` → `200`（存活，不依赖任何外部组件）
- `GET /readyz` → `503`（MySQL/Redis/Chroma 未起，`checks[]` 会精确指出失败项）
- `GET /metrics` → Prometheus 指标

> 依赖没起时应用**照样能启动**是刻意设计，见 ADR-0002：就绪与否交给 `/readyz`，避免
> compose/k8s 下的启动死锁与 CrashLoopBackOff。

## 2. 一键起停（Docker Compose）

要求：Docker Desktop 已启动。脚本兼容 **Windows PowerShell 5.1 与 PowerShell 7**（输出为 ASCII，
避免 5.1 把无 BOM 的 UTF-8 脚本按 ANSI 解析）：

```powershell
powershell -File scripts/up.ps1 -Build   # 起 api/worker/mysql/redis/chroma/nginx，并轮询 /readyz
powershell -File scripts/status.ps1      # 健康矩阵 + 探针明细
powershell -File scripts/down.ps1        # 停栈（加 -Volumes 连数据卷一起删）
# PowerShell 7 亦可：pwsh -File scripts/up.ps1 -Build
```

包含 MQ 与可观测栈：

```powershell
pwsh scripts/up.ps1 -WithMq -WithObs   # + RocketMQ namesrv/broker + Prometheus/Grafana/Alertmanager/Loki
# Grafana: http://127.0.0.1:3000 (admin/admin)  Prometheus: http://127.0.0.1:9090
```

启动顺序由 compose `depends_on: condition: service_healthy` 保证：
`mysql/redis/chroma` 健康 → `api`（`alembic upgrade head` 后起 uvicorn，健康检查过）→ `worker` → `nginx`。
api 与 worker **同一镜像、不同入口命令**（Spec §2.3）。

| 服务 | 地址 |
| --- | --- |
| API / Swagger | <http://127.0.0.1:8000/docs> |
| Nginx（SSE 不缓冲） | <http://127.0.0.1:8080/healthz> |
| Metrics | <http://127.0.0.1:8000/metrics> |
| MySQL / Redis / Chroma | 3306 / 6379 / 8100 |

## 3. 常用命令

```bash
make lint          # ruff check
make format        # ruff check --fix + ruff format
make type          # mypy app
make cov           # pytest + 覆盖率门禁（fail_under=80）
make ci            # 与 CI 完全一致的全量门禁（Windows: pwsh scripts/ci.ps1）
make compose-config
make smoke         # 起 uvicorn 打探针/metrics/trace 后退出
```

Windows 直接调用虚拟环境等价：

```powershell
.venv\Scripts\python.exe -m pytest -q
.venv\Scripts\python.exe -m ruff check .
.venv\Scripts\python.exe -m mypy app
```

## 4. 目录结构

```
app/
├── api/            # 路由、deps（鉴权/幂等/trace）、统一错误、SSE 协议、中间件
├── agent/          # LangGraph 状态与节点（M1/M2）、checkpointer
├── rag/            # 分块/索引/检索/缓存（阈值判定与 RRF 已实现纯函数）
├── tools/          # 工具清单（§8.1：5 只读 + 2 写，声明式注册表）
├── services/       # 状态机（§5.2 表→代码）、幂等、outbox、审批、健康检查
├── workers/        # worker 入口（心跳/优雅退出）+ 消费者契约
├── eval/           # 用例加载 + 发布门禁判定
├── db/             # SQLAlchemy 模型、会话
├── migrations/     # Alembic（9 张表，可 upgrade/downgrade）
└── common/         # 配置、日志、trace、指标、安全、客户端
deploy/             # Dockerfile、compose、nginx、prometheus/alerts、grafana、loki、alertmanager
scripts/            # up/down/status/ci/smoke
tests/              # unit / integration / e2e
evals/cases/        # 评测用例（四类，进 git）
docs/               # 需求分析、tickets、验证、ADR、Runbook
```

## 5. 接口速览（M0）

| 方法 | 路径 | M0 行为 |
| --- | --- | --- |
| `GET` | `/healthz` | 200，纯内存 |
| `GET` | `/readyz` | 依赖齐全 200；否则 503 + 逐项明细 |
| `GET` | `/metrics` | Prometheus 文本格式 |
| `POST` | `/api/v1/auth/login` | dev/test 签发 JWT；staging/prod 501（不臆造登录） |
| `POST` | `/api/v1/chat/stream` | SSE 骨架：`error`(AS-5002) + `done` |
| `POST` | `/api/v1/aftersales/apply` | 501（M2：状态机 + 幂等 + outbox） |
| `GET` | `/api/v1/aftersales/{biz_id}` | 501（M2） |
| `POST` | `/api/v1/approval/callback` | 501（M2：验签 + 幂等） |
| `POST` | `/api/v1/eval/run` | 501（M1） |
| `GET` | `/api/v1/traces/{trace_id}` | 501（M1） |
| `GET` | `/api/v1/orders/{order_id}` | 501（M2） |
| `*` | `/api/v1/admin/*` | 501（M3） |

统一响应：成功 `{"code":0,"message":"ok","data":…,"trace_id":…}`；
失败 `{"code":"AS-xxxx","message":…,"detail":{"milestone": …},"trace_id":…}`。
**每个响应都带 `X-Trace-Id`**，合法透传、非法替换。

## 6. 配置

- 唯一入口 `app/common/config.py`（pydantic-settings），字段与 `deploy/.env.example` 一一对应（有单测守护）。
- 密钥只接受**文件路径**（`JWT_PUBLIC_KEY_PATH` / `LLM_API_KEY_FILE`），不落镜像层、不进日志。
- `staging/prod` 使用默认开发密钥或缺少公钥路径 → **启动即失败**。
- 本地：`cp deploy/.env.example deploy/.env` 后按需修改（`.env` 已 gitignore）。

## 7. 里程碑与下一步

| 里程碑 | 内容 | 状态 |
| --- | --- | --- |
| M0 | 脚手架、compose、trace、统一异常、探针 | **本期交付** |
| M1 | RAG 索引/召回/引用作答、SSE 流式、会话与缓存、`/eval/run` | 待启动 |
| M2 | 工具 Provider、状态机落库、幂等锁、outbox、HITL 审批 | 待启动 |
| M3 | 告警值班、灰度回滚、备份恢复演练、压测、看板 API | 待启动 |
| M4 | 真实接口联调、安全与合规评审、灰度放量 | 待启动 |

详细拆解见 [docs/m0/03-verification.md](docs/m0/03-verification.md) 的「下一步迭代方案」章节。

## 8. 常见问题

| 现象 | 原因 | 处置 |
| --- | --- | --- |
| `/readyz` 503 且 `migrations` 失败 | 数据库未迁移 | `docker compose exec api alembic upgrade head` |
| `/readyz` 中 `mq` 为 `skipped` | 默认未启用 MQ（`MQ_ENABLED=false`） | 正常现象；需要时 `-WithMq` 起栈 |
| `docker compose up` 报 daemon 未运行 | Docker Desktop 没启动 | 先启动 Docker Desktop |
| 启动即退出，日志含「禁止使用默认开发 JWT 密钥」 | prod/staging 未配置密钥 | 设置 `JWT_PUBLIC_KEY_PATH` 与 RS256 |
| 端口冲突 | 3306/6379/8000 被占用 | 改 `deploy/.env` 里的 `*_PORT` |
