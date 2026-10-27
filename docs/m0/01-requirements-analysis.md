# M0（骨架）需求分析 · 电商售后管家

| 项 | 内容 |
| --- | --- |
| 依据 | 《电商售后管家 · 系统开发 Spec》v2.0（2026-10-27） |
| 里程碑 | M0 骨架（Spec §20） |
| 出口标准 | CI 绿灯，一键起停；工程脚手架、compose、trace/统一异常、`/healthz`+`/readyz` 就绪 |
| 分析日期 | 2026-10-27 |
| 结论 | M0 只交付"可运行、可观测、可扩展的骨架"，不实现问答/办事业务逻辑 |

---

## 1. 里程碑定位拆解

Spec §20 把 M0 定义为「工程脚手架、compose、trace/统一异常、`/healthz`+`/readyz`」，出口标准是「CI 绿灯，一键起停」。
把这句话翻译成可验收的工程约束，M0 实际必须同时满足三类需求：

| 类别 | 需求 | 为什么 M0 必须做 |
| --- | --- | --- |
| 结构需求 | 目录与分层严格对齐 §16.1，M1/M2 只"填肉"不改骨架 | 避免第 2 周返工重构 |
| 契约需求 | 统一响应信封、错误码、SSE 事件、trace 传播、API 路径前缀 | 前端/客服工作台要提前联调（§23.2） |
| 运行需求 | compose 一键起停、健康探针、结构化日志、指标端点、CI 门禁 | §22 Go-Live 清单里的可观测与发布能力，底座在 M0 |

一句话：**M0 交付"能起、能查、能看、能拦"的骨架**——进程能起、链路能查（trace_id）、指标能看、非法状态与错误能拦（统一异常）。

---

## 2. 需求分解（RA：Requirement Analysis）

> 编号 `RA-xx`，逐条给验收判定方式。凡与 Spec 有出入的地方，标注「偏差」并说明理由。

### RA-01 工程结构与依赖管理

- 需求：`app/{api,agent,rag,tools,services,workers,eval,common}`、`tests/{unit,integration,e2e}`、`deploy/`、`docs/` 与 §16.1 一致；Python 3.11；依赖锁定（§16.2「锁文件进 git」）；`ruff` + `mypy` + `pytest -cov` 均可在一条命令内跑通。
- 验收：`uv sync` 后 `python -c "import app.main"` 成功；`tests/` 目录齐备；`uv.lock` 入库。
- 说明：本机默认 Python 为 3.14，Spec 要求 3.11 → 用 `uv` 固定 3.11.15 虚拟环境，`.python-version` 入库，避免"本机能跑、CI 挂"。

### RA-02 统一配置（§16.2「仅 pydantic-settings 读环境变量」）

- 需求：所有配置来自环境变量/`.env`，字段命名与 §17.5 完全对齐（`DATABASE_URL`、`REDIS_URL`、`CHROMA_URL`、`MQ_NAMESERVER`、`RAG_TOP1_THRESHOLD`、`APPROVAL_THRESHOLD_CNY`、`GRAY_PERCENT`…）；密钥只允许"路径/文件"形态（`*_FILE` / `*_PATH`），禁止硬编码。
- 验收：非 dev 环境下若仍使用默认开发密钥 → 启动即失败（fail-fast）；`deploy/.env.example` 与 Settings 字段一一对应（有单测守护）。
- 偏差说明：Spec §17.5 给的是 prod 形态的环境变量全集，dev 侧额外需要 `JWT_HS256_SECRET`（本地免生成 RSA 密钥对），属 dev-only 增量，已在 `.env.example` 标注。

### RA-03 结构化日志与 trace 传播（§13.1）

- 需求：JSON line 日志，字段 `ts/level/trace_id/span/event/ms` 等；`trace_id` 由 `X-Trace-Id` 透传，缺省生成；长度 16–64、字符集 `[A-Za-z0-9_-]` 校验；响应头原样回写。
- 验收：任一请求的响应头含 `X-Trace-Id`；日志行可被 `json.loads` 解析且 `trace_id` 与响应头一致；非法 `X-Trace-Id`（过短/含非法字符）被替换为新 ID 且不报错（防日志注入）。
- 关键设计：`trace_id` 走 `contextvars`，异步任务与后台协程自动继承；`span` 用 `node.retrieve` / `tool.get_order` 这类点分命名，与 §13.1 示例一致。

### RA-04 统一响应与错误码（§9.1）

- 需求：成功 `{"code":0,"message":"ok","data":{...},"trace_id":"..."}`；失败 `{"code":"AS-xxxx","message":"...","detail":{...},"trace_id":"..."}`；实现 Spec 给出的 9 个业务错误码 + HTTP 状态映射；区分参数校验错误（AS-4001，422 输入不合法）与业务规则错误（AS-4002）。
- 验收：单测覆盖每个错误码的 HTTP 状态与响应体形状；未捕获异常 → AS-5001 且不泄漏堆栈到响应体（堆栈只进日志）；Pydantic 校验错误统一收敛为 AS-4001 并给出字段定位。

### RA-05 健康探针（§9.2、§17.6）

- 需求：
  - `GET /healthz`：存活探针，不依赖任何外部组件，必须毫秒级返回；
  - `GET /readyz`：就绪探针，校验 **MySQL / Redis / MQ / Chroma 连通 + 迁移版本**（§9.2 明确列出），任一失败返回 503 并给出逐项明细。
- 验收：无依赖启动时 `/healthz` 200、`/readyz` 503 且 `checks[]` 精确指出失败项；依赖齐全时 `/readyz` 200；单次探针总耗时受配置上限约束（默认 2s），不会因某个依赖 hung 住而拖垮 LB 健康检查。
- 关键设计：**探针不参与启动阻塞**。依赖未就绪时应用必须能启动（否则 compose 启动顺序死锁、k8s 下会 CrashLoop），就绪与否交给 `/readyz` 判定——这是与"启动即连库"方案的核心差别，也是 §17.6「启动顺序：依赖健康 → api → worker」能成立的前提。

### RA-06 API 路由骨架与契约（§9.2）

- 需求：`/api/v1` 下 chat / aftersales / approval / eval / traces / admin / auth / orders 路由组全部挂载；请求与响应 Pydantic 模型按 Spec 定义（含 SSE 事件协议枚举）；M0 未实现的业务返回 `AS-5002`（501）而不是 404 或裸 500。
- 验收：`/openapi.json` 可导出且包含 §9.2 全部路径；每个未实现端点返回统一错误信封且 `code=AS-5002`；SSE 端点返回 `text/event-stream` 且 M0 至少推送 `error`+`done` 事件骨架（保证前端联调不被 500 打断）。
- 偏差说明：Spec 错误码表无 501 语义，新增 `AS-5002 NOT_IMPLEMENTED` 作为内部扩展并在错误码表登记，避免与 `AS-5001 内部错误`混用。

### RA-07 鉴权依赖（§9.1、§14）

- 需求：`Authorization: Bearer <JWT>`，payload 含 `user_id/role/exp`；角色 `buyer/agent/approver/ops/admin`；提供 `require_roles(...)` 依赖；生产用 RS256 公钥文件，dev/test 用 HS256。
- 验收：无 token / 过期 token / 角色不足分别命中 `AS-4010`(401) / `AS-4010` / `AS-4030`(403)；`/auth/login` 仅在 dev/test 签发令牌，staging/prod 返回 `AS-5002`（真实登录对接用户中心属 M4，Spec 未定义用户源，不臆造）。
- 说明：这是 M0 唯一"故意不做全"的能力，已在 README 与 tickets 标注 TODO(M4)。

### RA-08 数据层与迁移（§10.1、§17.3）

- 需求：SQLAlchemy 2.0 async + aiomysql；Alembic 迁移可执行；初始迁移覆盖 §10.1 的 9 张表（`aftersales_biz`、`aftersales_transition`、`approval_task`、`outbox_message`、`index_version`、`prompt_registry`、`eval_case`、`eval_run`、`tool_audit`），含唯一键/索引/乐观锁字段；`/readyz` 校验 `alembic_version` 与脚本 head 一致。
- 验收：`alembic upgrade head` 在 compose 的 MySQL 8.0 上成功；`alembic downgrade base` 可回退；ORM 模型表名集合与迁移表名集合相等（单测守护），避免"模型改了迁移没改"。
- 说明：连接串形如 `mysql+aiomysql://...`，Alembic 需要同步驱动语义 → 用 `async_engine` + `run_sync` 的 async env，避免再引入 pymysql。

### RA-09 状态机与纯函数内核（§5.2、§8.1、§7.2）

- 需求：把 Spec 里**不可测的表格**变成**可测的代码常量**——状态迁移表（§5.2）、工具清单（§8.1）、检索阈值判定（§7.2）、缓存键规范（§7.3/§11.1）、outbox 消息信封（§12.2）。M0 只做纯函数与数据，不接 I/O。
- 验收：`validate_transition` 覆盖表内全部合法迁移 + 表外全部非法迁移（含终态自环）；非法迁移抛 `IllegalTransitionError`（`AS-4003`）；工具清单 7 个（5 只读 + 2 写）；阈值判定三档边界（0.75/0.55）用参数化单测锁定。
- 理由：M0 用测试把"唯一权威来源"钉死，M2 写状态机时只需接 DB（乐观锁 + 事务），不会出现"代码与表格不一致"的经典事故（§23.2 第一条检查项）。

### RA-10 Compose 一键起停与镜像（§17.6）

- 需求：`deploy/docker-compose.yml` 含 api/worker/mysql/redis/chroma/nginx，用 profile 隔离重型依赖（rocketmq、可观测栈）；同镜像双入口（`uvicorn app.main:app` / `python -m app.workers.runner`）；健康检查与 `depends_on: condition: service_healthy`；`deploy/.env.example` 完整。
- 验收：`docker compose config` 无警告通过；`scripts/up.ps1` 一条命令起停并打印各服务健康；api 容器 `/healthz` 通过自身 HEALTHCHECK。
- 偏差说明：Spec 要求 RocketMQ，但 RocketMQ 单机（namesrv+broker）镜像体积大、Windows 上桥接网络易踩坑 → 用 `--profile mq` 隔离，默认 dev 栈不含 MQ，`/readyz` 在 `MQ_ENABLED=false` 时把 MQ 检查标记为 `skipped`（仍然出现在 checks 明细里，不伪装通过）。

### RA-11 CI 门禁（§16.3）

- 需求：`ruff check`、`mypy`、`pytest --cov`（核心模块覆盖 ≥ 80%）、`docker compose config` 校验、镜像/进程冒烟（起服务 → 打 `/healthz`）。
- 验收：本地执行与 CI 相同命令全绿；覆盖率门禁在 `pyproject.toml` 中 `fail_under=80`；CI 在 PR 上阻断失败。
- 说明：trivy 漏洞扫描与 JMeter 门禁分别在 M3 接入（需镜像仓库与压测环境），M0 只留 workflow 占位与文档说明。

### RA-12 文档与运行手册

- 需求：README（快速开始/目录结构/命令）、ADR（关键决策：Python 版本、探针语义、错误信封、MQ 可选）、M0 验收清单、Runbook 索引（对齐 §19）。
- 验收：新同学按 README 从零到 `/readyz` 全绿 ≤ 15 分钟（不含镜像拉取）。

---

## 3. 明确不做（M0 范围外）

| 项 | 归属 | 理由 |
| --- | --- | --- |
| RAG 索引构建、混合召回、Reranker、引用作答 | M1 | 需真实 embedding 与语料 |
| LangGraph 图装配与节点实现 | M1/M2 | 依赖 Prompt 与工具契约稳定 |
| 工具真实调用（订单/物流/退款 Provider） | M2/M4 | Q1 未联调，M0 只定 schema |
| 幂等/锁/outbox dispatcher 的 DB 实现 | M2 | M0 只定 key 规则与信封 |
| 灰度、熔断、Prompt 发布、看板 API 的真实逻辑 | M3 | 需管理面与审计表落地 |
| 前端工作台 | 二期 | Spec §1.3 明确范围外 |

---

## 4. 关键风险与 M0 处置

| 风险 | 影响 | M0 处置 |
| --- | --- | --- |
| 依赖不可用导致启动失败 | compose 起不来、k8s CrashLoop | 探针化就绪（RA-05），启动不连库 |
| 本机 Python 3.14 与 Spec 3.11 不一致 | CI 与本地行为漂移 | `uv` 锁定 3.11.15 + `.python-version` |
| MQ 在 Windows dev 不稳 | 一键起停失败 | profile 隔离 + `MQ_ENABLED` 开关 + 探针 `skipped` 语义 |
| 骨架与 Spec 表格漂移 | M2 状态机事故 | 表格转代码常量 + 参数化单测（RA-09） |
| 未实现端点被误认为已完成 | 联调误判 | 统一 501 + `AS-5002` + OpenAPI 标注 `M1/M2` |

---

## 5. M0 验收清单（可执行）

```powershell
# 1. 环境
uv sync --extra dev
# 2. 静态门禁
uv run ruff check . ; uv run mypy app
# 3. 单测 + 覆盖率门禁
uv run pytest -q --cov=app --cov-report=term-missing
# 4. 编排校验
docker compose -f deploy/docker-compose.yml config -q
# 5. 一键起停 + 探针
pwsh scripts/up.ps1            # 起依赖+api+worker
curl.exe -s localhost:8000/healthz
curl.exe -s localhost:8000/readyz | ConvertFrom-Json | ConvertTo-Json -Depth 5
pwsh scripts/down.ps1
```

判定：2–4 全绿，5 中 `/healthz` 200、`/readyz` 200（依赖齐）/503（依赖缺，明细可读）。
