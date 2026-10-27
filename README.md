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
- 密钥只接受**文件路径**（`JWT_PUBLIC_KEY_PATH` / `LLM_API_KEY_FILE`），不落镜像层、不进日志；
  密钥文件支持纯密钥或 `LLM_KEY=sk-...` 两种写法。
- `staging/prod` 使用默认开发密钥、缺公钥路径、或真实 LLM 缺 key 文件 → **启动即失败**。
- 两套 env 文件互不干扰：
  - **本地进程**：读仓库根 `.env`（`cp deploy/.env.example .env`）；
  - **compose**：读 `deploy/.env` 只用于变量插值，服务配置由 compose `environment:` 注入。

### 6.1 接真实模型（M1）

```bash
# 1) key 只放文件（已在 .gitignore）
mkdir -p .secrets && echo "LLM_KEY=sk-你的key" > .secrets/llm_key.txt

# 2) 本地 .env 覆盖（示例）
cat >> .env <<'EOF'
LLM_PROVIDER=openai
LLM_BASE_URL=https://api.deepseek.com
LLM_MODEL=deepseek-flash
LLM_API_KEY_FILE=.secrets/llm_key.txt
EMBEDDING_PROVIDER=fastembed
EMBEDDING_DIM=512
HF_ENDPOINT=https://hf-mirror.com
EOF

# 3) 验真（默认跳过；本地会真调模型，成本极低）
uv run pytest tests/integration/test_llm_live.py -q
# 4) 验本地语义向量（首次下载 ~100MB，耐心等 1 分钟）
RUN_LOCAL_EMBED=1 uv run pytest tests/integration/test_llm_live.py -q -k fastembed
```

| 变量 | 说明 |
| --- | --- |
| `LLM_PROVIDER` | `mock`（CI/离线回放，缺录制即失败）/ `openai`（任意 OpenAI 兼容网关） |
| `LLM_MODEL` | DeepSeek 实测**必须用** `deepseek-flash`（`DeepSeek-V4.1-Flash` 只是展示名） |
| `LLM_REASONING_EFFORT` | `low/high/max`；推理模型档位，`low` 可明显降低首字延迟 |
| `LLM_FALLBACK_MODEL` | 主模型 5xx/超时后自动切备 |
| `EMBEDDING_PROVIDER` | `hash`（CI，确定性但无语义）/ `fastembed`（本地 bge-small-zh，512 维）/ `api` |
| `HF_ENDPOINT` | 模型下载镜像；本机直连 huggingface.co 超时，实测 `https://hf-mirror.com` 可用 |

> 依赖说明：本地向量需要 `uv sync --extra local-embed`。`onnxruntime` 钉在 `<1.21`——
> 1.31 在本机（Win11 / i5-1240P / numpy 2.4）导入即 access violation，1.20.1 正常。

## 7. 里程碑与下一步

| 里程碑 | 内容 | 状态 |
| --- | --- | --- |
| M0 | 脚手架、compose、trace、统一异常、探针 | ✅ 已交付 |
| M1 | RAG 索引/召回/引用作答、SSE 流式、会话与缓存、`/eval/run` | 🚧 进行中（I1：provider 已落地） |
| M2 | 工具 Provider、状态机落库、幂等锁、outbox、HITL 审批 | 待启动 |
| M3 | 告警值班、灰度回滚、备份恢复演练、压测、看板 API | 待启动 |
| M4 | 真实接口联调、安全与合规评审、灰度放量 | 待启动 |

- M0 验收记录：[docs/m0/03-verification.md](docs/m0/03-verification.md)
- M1 方案与进度：[docs/m1/01-plan.md](docs/m1/01-plan.md) · [docs/m1/02-tickets.md](docs/m1/02-tickets.md)

## 8. RAG 索引与检索（M1）

```bash
# 离线检索：内存索引，不依赖 MySQL/Chroma；embedding 走当前配置（fastembed 时是真实语义分）
uv run python -m app.rag.cli query "退款多久到账" --offline
uv run python -m app.rag.cli query "退货运费谁承担" --offline --json

# 在线：构建索引（写 Chroma + rag_chunk 并激活版本），再检索
uv run python -m app.rag.cli rebuild --version v1
uv run python -m app.rag.cli query "退款多久到账" --version v1
uv run python -m app.rag.cli versions
```

输出包含三档判定（`answer` / `answer_with_caveat` / `fallback`）、Top-K 的 `vector_score`、
`keyword_score`、出处（doc/section）与降级原因。**离线模式只验证管道，不代表检索质量**。

语料在 `seeds/corpus/`（示例语料，M4 替换真实文档）；分块规则 500 字 / 80 overlap、
按 `##`/`###` 标题切节；`rag_chunk` 由迁移 0002 建立，关键词通道用 MySQL `FULLTEXT ... WITH PARSER ngram`。

## 9. 常见问题

| 现象 | 原因 | 处置 |
| --- | --- | --- |
| `/readyz` 503 且 `migrations` 失败 | 数据库未迁移 | `docker compose exec api alembic upgrade head` |
| `/readyz` 中 `mq` 为 `skipped` | 默认未启用 MQ（`MQ_ENABLED=false`） | 正常现象；需要时 `-WithMq` 起栈 |
| `docker compose up` 报 daemon 未运行 | Docker Desktop 没启动 | 先启动 Docker Desktop |
| 启动即退出，日志含「禁止使用默认开发 JWT 密钥」 | prod/staging 未配置密钥 | 设置 `JWT_PUBLIC_KEY_PATH` 与 RS256 |
| 端口冲突 | 3306/6379/8000 被占用 | 改 `deploy/.env` 里的 `*_PORT` |
