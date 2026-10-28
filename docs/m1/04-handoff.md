# M1 交接文档 · 当前进度与下一步

> **用途**：下次继续开发时先读这一份，再读 `docs/m1/01-plan.md`（方案）与 `docs/m1/03-verification.md`（验证记录）。
> 最后更新：2026-10-28 12:05（本地时间）· 对应提交见文末。

---

## 1. 一句话状态

| 里程碑 | 状态 |
| --- | --- |
| M0 骨架 | ✅ 完成 |
| M1 I1 检索闭环 | ✅ 完成（provider / 语料 / 索引 / 混合召回 / 三档阈值） |
| M1 I2 对话闭环 | ✅ 完成（Prompt / 图 / SSE 续传 / 记忆 / 缓存） |
| M1 I3 质量与可观测 | ✅ **M1-10~15 全部完成；评测 90/90（100%）；热命中 6ms / 0 token；覆盖率 85.01%**；仅剩 staging 复测 |

**最新真实评测（离线：真实 DeepSeek + 本地 bge 向量，90 条；2026-10-28）**

| 指标 | 数值 | 门禁 | 结论 |
| --- | --- | --- | --- |
| 完成率 | **100%（90/90）** | ≥85% | ✅ |
| 引用正确率 | **100%** | ≥95% | ✅ |
| 无答案兜底 | **15/15** | 100% | ✅ |
| P95 | **4647ms** | 单轮生成另计 | — |
| 失败归因 | 空 | — | 越界问题由可答性判定兜底 |

---

## 2. 已完成（可直接使用）

### 2.1 工程与基础设施（M0）

- 分层脚手架、`compose`（9 服务 + mq/obs profile）、nginx（SSE 不缓冲）、Prometheus 告警、Grafana/Loki；
- 统一响应信封与 12 个错误码（`AS-5002` 未实现、`AS-5033` 上游模型不可用等）；
- trace 中间件、结构化 JSON 日志（敏感键打码/保留字转义）、`/healthz`、`/readyz`、`/metrics`；
- Alembic：9 张 M0 表 + `rag_chunk`（ngram FULLTEXT）+ `trace_span`；
- JWT（dev HS256 / prod RS256）、幂等、状态机迁移表、outbox；
- CI 5 job 全绿（quality / smoke / compose+trivy / 真实 MySQL 迁移 / 真实 MySQL+Chroma+Redis 的 RAG 端到端）。

### 2.2 问答链路（I1 + I2）

| 能力 | 落点 |
| --- | --- |
| LLM 抽象 | `app/llm/*`（OpenAI 兼容 DeepSeek、mock 回放、推理内容分离、主备降级） |
| Embedding | `app/llm/embeddings.py`（hash / fastembed bge-small-zh / API） |
| 语料与分块 | `seeds/corpus/*.md`、`app/rag/chunker.py`（500/80、标题层级） |
| 索引 | `app/rag/indexer.py`、`app/rag/chroma.py`（版本化、覆盖写 `rag_chunk`） |
| 检索 | `app/rag/pipeline.py`（向量 + ngram 关键词 → RRF → rerank → 三档阈值 + 降级链） |
| Prompt | `app/prompt/*`（DB→seed 回落、`$var`、注入防御、出口脱敏） |
| 图 | `app/agent/graph.py` + `nodes/core.py`（7 节点 + pending_capability，节点级 span） |
| SSE | `app/api/routers/chat.py` + `sse_buffer.py`（delta/citation/clarify/done、`Last-Event-ID` 回放） |
| 记忆 | `app/session/*`（10 轮窗口 + 溢出归档 + 摘要压缩） |
| 缓存 | `app/cache/two_level.py`（L1 60s / L2 300s + 单飞 + 版本前缀失效） |
| **评测** | `app/eval/*` + `evals/cases/*.json`（90 条、断言/归因、`/eval/run`、离线 CLI） |
| **调用树** | `app/services/trace_store.py` + 迁移 0003 + `GET /traces/{id}` |

### 2.3 评测调优的真实修复（不是改用例）

1. router prompt 增加"政策问答 / 发起办理 / 指代查订单"边界 + few-shot；
2. rewrite prompt 增加 few-shot 与"必须问号结尾"；
3. 改写**确定性兜底** `rule_rewrite`：模型原样返回时拼上一轮话题；
4. answer prompt 强化"每个事实句必须带 `[[n]]`，无编号视为无效"；
5. query 意图在 M1 走 `pending_capability`，不检索不改写——原先用例期望写错，已修正并注明理由。

### 2.4 已修的真实问题（避免重复踩坑）

1. `aiomysql` 连 MySQL 8 需要 `cryptography`；
2. `.dockerignore` 曾排除 `README.md`；
3. `AsyncRedisSaver` TTL 单位是**分钟**，且必须 Redis Stack；
4. `onnxruntime 1.31` 本机导入崩溃 → 钉 `>=1.17,<1.21`；
5. `deploy/.env` 会覆盖根 `.env`；
6. PEM 密钥被首行截断 → 识别 `-----BEGIN`；
7. 状态存枚举触发 LangGraph serde 警告 → 全部存 JSON 原生类型；
8. 流式脱敏按句切分，避免手机号跨 chunk 泄露半截；
9. 2-gram 覆盖率阈值**不可行**（域内/域外分布重叠），已回滚。

---

## 3. 未完成（按优先级）

### P1 · M1 收尾（只剩真实栈验证）

| 事项 | 状态 / 建议 |
| --- | --- |
| staging 真实栈评测（MySQL + Chroma + 正式语料，90 条） | 本机 Docker Hub 不可达；需在有 Docker 的环境跑 `python -m app.eval.cli run --suite nightly`（非 `--offline`） |
| 首片段 P95 压测 | 指标 `chat_first_token_seconds{mode}` 已接线；需在 staging 用缓存命中/未命中两组样本出曲线 |
| CI 全绿确认 | 本地 506 passed / 83.29%；CI run #19 已 6/6 全绿 |
| 无答案 100% 兜底 | ✅ 15/15（可答性判定器，仅低分 caveat 触发、fail-open） |
| 缓存收益 ≥60% / 端到端 P95 ≤1.5s（缓存命中） | ✅ 答案级缓存 + 入口短路；实测 2814ms/875 tokens → 6ms/0 tokens |
| `rag/`+`agent/` 覆盖率 ≥85% | ✅ 85.01%（全局 84.92%） |

### P0 · 已无代码项（M1 出口门禁全绿）

| 事项 | 说明 |
| --- | --- |
| staging 真实栈复测 | 仅剩的验证项：真实 MySQL+Chroma+正式语料跑 90 条 + 并发压测；本机 Docker Hub 不可达 |

### P2 · 已知质量缺口（M2）

- **5 条越界问题未兜底**（海外仓维修 / 延保购买 / 上门安装 / 会员积分兑换 / 线下门店售后）：
  检索给 0.55-0.61 落入"带保留作答"档，模型据此作答。
  方向：可答性分类器（LLM 判定"资料是否覆盖"）或语料覆盖索引；**不要**再用字符覆盖率硬阈值。
- `eval_run` 落库与 trace 清理 TTL：staging/M3。

---

## 4. 下次怎么继续（复制即用）

```powershell
cd D:\AI\e-co

# 1) 门禁
.venv\Scripts\python.exe -m ruff check .
.venv\Scripts\python.exe -m ruff format --check .
.venv\Scripts\python.exe -m mypy app
.venv\Scripts\python.exe -m pytest -q --cov=app

# 2) 真实离线评测（约 3-4 分钟）
.venv\Scripts\python.exe -m app.eval.cli run --offline --suite local --no-fail-on-gate

# 3) 只跑多轮/多跳
.venv\Scripts\python.exe -m app.eval.cli run --offline --categories multi_turn,multi_hop

# 4) 检索单点调试
.venv\Scripts\python.exe -m app.rag.cli query "退款多久到账" --offline --json

# 5) 真实栈（需要 Docker Hub 可达）
docker compose -f deploy/docker-compose.yml up -d mysql redis chroma
.venv\Scripts\python.exe -m alembic upgrade head
.venv\Scripts\python.exe -m app.rag.cli rebuild --version v1
```

**环境要点**

- `.env`（gitignored）：`LLM_PROVIDER=openai`、`LLM_MODEL=deepseek-flash`、`LLM_API_KEY_FILE=.secrets/llm_key.txt`、`EMBEDDING_PROVIDER=fastembed`、`HF_ENDPOINT=https://hf-mirror.com`；
- 本机 Docker Hub 仍不可达 → 真实栈验证靠 CI；本地用 `--offline` 评测；
- PowerShell 5.1：`.ps1` 保持 ASCII；
- 提交身份：`git -c user.name="yoruLearn" -c user.email="2147140881@qq.com"`。

---

## 5. 关键文件索引

```text
docs/m1/01-plan.md            M1 方案
docs/m1/02-tickets.md         工单状态
docs/m1/03-verification.md    验证记录（含 4 轮评测数字）
docs/m1/04-handoff.md         本文件

app/agent/{graph,runtime,state,citations,checkpointer}.py
app/agent/nodes/core.py      7 节点 + 改写兜底
app/api/routers/{chat,traces}.py
app/api/sse_buffer.py        帧缓冲
app/rag/{chunker,indexer,chroma,keyword,pipeline}.py
app/prompt/{registry,guard}.py
app/session/{store,summarize}.py
app/cache/two_level.py
app/eval/{assertions,executor,graph_executor,offline,cli,store}.py
app/services/trace_store.py
app/migrations/versions/0003_trace_span.py
evals/cases/*.json           90 条用例
seeds/prompts.yaml           Prompt 基线（router/rewrite/answer 已调优）
```

---

## 6. 最近提交

| 提交 | 内容 |
| --- | --- |
| `640b317` | 七节点图 + SSE（引用/脱敏/兜底）+ 记忆 |
| `7de6611` | SSE 续传 + 摘要压缩 + 两级缓存与单飞 |
| `d78a8f5` | 评测执行器 + 90 条用例 + `/eval/run` + 交接文档 |
| `9e099fd` | 评测调优（81.11%→94.44%）+ M1-11 trace_span 调用树 |
| 本次 | M1-12 指标/看板 + M1-13 限流与会话互斥 + M1-14 CI 评测抽样 |
