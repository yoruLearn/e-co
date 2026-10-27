# M1 交接文档 · 当前进度与下一步

> **用途**：下次继续开发时先读这一份，再读 `docs/m1/01-plan.md`（方案）与 `docs/m1/03-verification.md`（验证记录）。
> 最后更新：2026-10-27 23:20（本地时间）· 对应提交见文末。

---

## 1. 一句话状态

| 里程碑 | 状态 |
| --- | --- |
| M0 骨架 | ✅ 完成（CI 5 job 全绿，已上线仓库） |
| **M1 I1 检索闭环** | ✅ 完成（provider / 语料 / 索引 / 混合召回 / 双阈值） |
| **M1 I2 对话闭环** | ✅ 完成（Prompt / 图 / SSE 续传 / 记忆 / 缓存） |
| **M1 I3 质量与可观测** | 🚧 进行中：**M1-10 评测代码与 90 条用例已完成，但质量门禁未过**；M1-11/12/13/14 未开始 |

**当前结论：M1 还不能宣布完成。** 真实离线评测（真实 DeepSeek + 本地 bge 向量）：
完成率 **81.11%（73/90）**、引用正确率 **96.23%**、P95 **4.67s**，低于门禁（完成率 ≥85% / 90%），
失败归因：`rewrite 6` / `retrieve 5` / `router 4` / `answer 2`。

---

## 2. 已完成（可直接使用）

### 2.1 工程与基础设施（M0）

- 分层脚手架、`compose`（9 服务 + mq/obs profile）、nginx（SSE 不缓冲）、Prometheus 告警规则、Grafana/Loki；
- 统一响应信封与 12 个错误码（含本项目扩展 `AS-5002` 未实现、`AS-5033` 上游模型不可用）；
- trace 中间件（`X-Trace-Id` 透传/替换）、结构化 JSON 日志（敏感键打码、LogRecord 保留字转义）；
- `/healthz`、`/readyz`（MySQL/迁移/Redis/MQ/Chroma 逐项并发探测）、`/metrics`；
- Alembic 迁移：9 张 M0 表 + `rag_chunk`（含 `FULLTEXT ... WITH PARSER ngram`）；
- JWT（dev HS256 / prod RS256 + 密钥文件）、幂等键规则、状态机迁移表（Spec §5.2 全量编码）、outbox 信封；
- CI 5 个 job 全绿：quality、smoke、compose+trivy、真实 MySQL 迁移、**真实 MySQL+Chroma+Redis 的 RAG 端到端**。

### 2.2 问答链路（M1 I1 + I2）

| 能力 | 落点 | 说明 |
| --- | --- | --- |
| LLM 抽象 | `app/llm/*` | `ChatModel` 协议；OpenAI 兼容实现（DeepSeek 实测）；mock 录制回放；推理模型 `reasoning_content` 与正文分离；主备模型降级 |
| Embedding | `app/llm/embeddings.py` | hash（CI 离线）/ fastembed bge-small-zh（512 维，实测可用）/ API |
| 语料 + 分块 | `seeds/corpus/*.md`、`app/rag/chunker.py` | 5 篇示例语料（3 政策 + 40 FAQ + 1 手册）；500/80、标题层级、句子优先 |
| 索引 | `app/rag/indexer.py`、`app/rag/chroma.py` | Chroma REST + 32/批向量化 + 覆盖写 `rag_chunk` + 版本 building→active→deprecated |
| 检索 | `app/rag/pipeline.py` | 向量 Top20 + MySQL ngram Top20 → RRF Top10 → Rerank Top5 → 三档阈值；降级链 |
| Prompt | `app/prompt/*`、`seeds/prompts.yaml` | DB 优先、seed 回落、`$var` 模板、注入防御（闭合标签转义）、出口脱敏（手机/身份证/银行卡/邮箱） |
| 图 | `app/agent/graph.py`、`app/agent/nodes/core.py` | 7 节点 + `pending_capability`；意图四分类；规则兜底；无引用拦截 |
| SSE | `app/api/routers/chat.py`、`app/api/sse_buffer.py` | `status/delta/citation/clarify/error/done`；会话级 `id`；`Last-Event-ID` 回放不重新生成 |
| 记忆 | `app/session/*` | 窗口 10 轮 + 溢出归档 + 摘要压缩；Redis 故障降级 |
| 缓存 | `app/cache/two_level.py` | L1 60s / L2 300s + 单飞；索引版本前缀失效 |
| 评测 | `app/eval/*`、`evals/cases/*.json` | 90 条用例（40/15/15/20）、断言 + 归因、门禁、`POST /eval/run`、离线 CLI |

### 2.3 已修的真实问题（避免重复踩坑）

1. `aiomysql` 连 MySQL 8 需要 `cryptography`（同时是 PyJWT RS256 的后端）；
2. `.dockerignore` 曾排除 `README.md` 导致镜像构建失败；
3. LangGraph `AsyncRedisSaver` **TTL 单位是分钟**，且需要 **Redis Stack**（RedisJSON + RediSearch）；
4. `onnxruntime 1.31` 在本机导入即 access violation → 钉 `>=1.17,<1.21`（1.20.1 正常）；
5. `deploy/.env` 会覆盖根 `.env` → 配置只读根 `.env`；
6. PEM 密钥被"首行优先"截断 → 识别 `-----BEGIN`；
7. 状态里存枚举会触发 LangGraph serde 警告（未来版本拒绝反序列化）→ `intent/status/citations` 全部存 JSON 原生类型；
8. 流式脱敏按句切分，避免手机号跨 chunk 泄露半截。

---

## 3. 未完成（按优先级）

### P0 · 让 M1 门禁变绿（必须先做）

| # | 事项 | 现状 / 下一步 |
| --- | --- | --- |
| 1 | **完成率 81.11% → ≥85%** | 失败集中在 `multi_turn`（改写未变化 6 条、意图误判 4 条）。先查 rewrite prompt 输出是否被模型回成了"答案"而非"改写问句"，再看 router 的 few-shot 是否缺少指代类样本 |
| 2 | `retrieve` 归因 5 条 | 多跳用例要求 `min_citations=2`，当前 Top-K 只命中 1 条；可提高 `RAG_TOP_K_FINAL` 或调整 RRF/阈值 |
| 3 | `answer` 归因 2 条 | 关键词或引用缺失；核对提示词对"必须标注引用"的约束是否被 reasoning 稀释 |
| 4 | 门禁阈值口径 | 代码默认 `completion_rate=0.90`，Spec §20 写 ≥85%；先统一口径并在 `evaluate_gate` 注释里写清，再谈通过 |

### P1 · I3 剩余工单（M1 出口）

| ID | 事项 | 备注 |
| --- | --- | --- |
| M1-11 | `trace_span` 表 + 节点级 span + `GET /traces/{id}` 调用树 | 迁移 0003；节点计时可在 `build_graph` 包一层 |
| M1-12 | 指标接线 + Grafana 看板 v1 | `chat_latency_seconds`、`rag_cache_hit_ratio`、`llm_tokens_total`、`answer_without_citation_total`；缓存命中率已在 `TwoLevelCache.stats` |
| M1-13 | 限流（20rpm/用户）+ 单会话并发 1 + 输入防护收口 | Redis 滑动窗口；429 `AS-4290` |
| M1-14 | CI 加评测抽样 job + 文档收尾 | 用 mock/离线子集，不进真实模型（控制成本） |

### P2 · 已知但不在本次范围

- staging 真实栈评测（真实 MySQL + Chroma + 正式语料）；
- 首 token P95 ≤800ms：推理模型首正文 ~2.2s，仅缓存命中路径可达成，需要 M1-12 的实测曲线支撑；
- `deploy` 的 Redis Stack 镜像切换未在本机验证（Docker Hub 不可达），CI 已用同一镜像验证 checkpointer；
- 首 token 分段指标、SLO 看板、告警触达（M3）。

---

## 4. 下次怎么继续（复制即用）

```powershell
cd D:\AI\e-co

# 1) 环境自检（应全绿）
.venv\Scripts\python.exe -m pytest -q --cov=app
.venv\Scripts\python.exe -m ruff check .
.venv\Scripts\python.exe -m mypy app

# 2) 跑评测（离线，真实模型 + 本地向量；约 3-4 分钟/90 条）
.venv\Scripts\python.exe -m app.eval.cli run --offline --suite local --no-fail-on-gate

# 3) 只跑多轮/多跳（定位失败）
.venv\Scripts\python.exe -m app.eval.cli run --offline --categories multi_turn,multi_hop

# 4) 检索单点调试（不需要 DB）
.venv\Scripts\python.exe -m app.rag.cli query "退款多久到账" --offline --json

# 5) 真实栈（需要 Docker Hub 可达）
docker compose -f deploy/docker-compose.yml up -d mysql redis chroma
.venv\Scripts\python.exe -m alembic upgrade head
.venv\Scripts\python.exe -m app.rag.cli rebuild --version v1
```

**环境要点**

- `.env`（gitignored）已指向真实网关：`LLM_PROVIDER=openai`、`LLM_MODEL=deepseek-flash`、`LLM_API_KEY_FILE=.secrets/llm_key.txt`、`EMBEDDING_PROVIDER=fastembed`、`HF_ENDPOINT=https://hf-mirror.com`；
- key 文件格式 `LLM_KEY=sk-...`，不要提交；
- 本机 Docker Hub 当前不可达（`registry-1.docker.io:443` 超时），本地起不了 MySQL/Chroma；CI 可拉镜像；
- Windows 只有 PowerShell 5.1：脚本已改 ASCII 输出，别在 `.ps1` 里写中文。

---

## 5. 关键文件索引

```text
docs/m1/01-plan.md            M1 方案（决策/工单/风险）
docs/m1/02-tickets.md         工单状态表
docs/m1/03-verification.md    逐轮验证记录（含真实评测数字）
docs/m1/04-handoff.md         本文件
docs/adr/0001-*.md            M0 架构决策
docs/adr/0002-*.md            LangGraph + checkpointer 决策（TTL/Redis Stack）

app/agent/{graph,runtime,state,citations,checkpointer}.py
app/agent/nodes/core.py       7 个节点
app/api/routers/chat.py       SSE + 续传
app/api/sse_buffer.py         帧缓冲
app/rag/{chunker,indexer,chroma,keyword,pipeline}.py
app/prompt/{registry,guard}.py
app/session/{store,summarize}.py
app/cache/two_level.py
app/eval/{assertions,executor,graph_executor,offline,cli,store}.py
evals/cases/*.json            90 条用例（40/15/15/20）
seeds/corpus/*.md             示例语料
seeds/prompts.yaml            Prompt 基线
```

---

## 6. 最近提交

| 提交 | 内容 |
| --- | --- |
| `640b317` | LangGraph 七节点图 + 真实 SSE（引用/脱敏/兜底）+ 会话记忆 |
| `7de6611` | SSE 断线续传 + 会话摘要压缩 + 两级缓存与单飞（I2 收尾） |
| 本次 | 评测执行器 + 90 条用例 + `POST /eval/run` + 真实离线评测报告 |
