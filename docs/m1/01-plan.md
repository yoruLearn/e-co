# M1 方案 · 问答链路（RAG + LangGraph + SSE）

| 项 | 内容 |
| --- | --- |
| 里程碑 | M1（Spec §20：第 2–3 周） |
| 依据 | Spec §6（Agent 编排）、§7（RAG）、§9（API/SSE）、§13（评测与可观测）、§15（SLO） |
| 当前基线 | M0 已交付：探针、trace、统一异常、9 表 DB、状态机/工具/阈值/缓存键/outbox 纯函数层、280 单测全绿、CI 4 job 全绿 |
| 结论（决策已确认） | 接入真实 OpenAI 兼容网关 + 本地 fastembed + 我编写示例语料；「先打通最小闭环，再补全能力」的纵向切片，3 周 / 15 张工单 |

---

## 1. 阶段目标与出口标准

**一句话目标**：让 `POST /api/v1/chat/stream` 真正可用——知识类问题走「混合召回 → 重排 → 阈值判定 → 带引用作答」，全程可流式、可缓存、可评测、可回溯。

**出口标准（Spec §20 / §13.3）**

| 维度 | 门禁 | 测量方式 |
| --- | --- | --- |
| 完成率 | ≥ 85%（典型类） | `/eval/run` 跑 90 条用例 |
| 引用正确率 | ≥ 95% | 引用块与 golden doc/section 比对 |
| 无答案 | 100% 兜底/拒绝，零编造 | no_answer 15 条必须命中兜底话术 |
| 首 token 延迟 | P95 ≤ 800ms（热缓存） | Prometheus `chat_latency_seconds` + 首 token 分段指标 |
| 端到端延迟 | P95 ≤ 1.5s（缓存命中） | 同上 |
| 缓存收益 | Embedding/LLM 调用下降 ≥ 60% | 压测前后计数器对比（FR-CACHE-04） |
| 覆盖率 | `rag/`+`agent/` ≥ 85%，全局 ≥ 80% | pytest-cov |
| 回归 | M0 的 4 个 CI job 保持全绿 | GitHub Actions |

> **诚实边界**：完成率/引用率只有接真实 LLM 才有意义。若暂时没有可用的模型网关，M1 交付"管道完成 + mock 基线"，质量门禁转为 staging 验收项（方案 §7 已隔离）。

---

## 2. 前置决策（已确认）

| # | 决策 | 结论 | 落地方式 | 影响 |
| --- | --- | --- | --- | --- |
| D1 | LLM 来源 | ✅ **接真实 OpenAI 兼容网关** | `LLM_PROVIDER=openai` + `LLM_BASE_URL` + `LLM_API_KEY_FILE`（key 放 `.secrets/`，已 gitignore）；同时保留 `mock` 供 CI 离线回放 | 决定 M1 能否真实验收质量门禁 |
| D2 | Embedding | ✅ **本地 fastembed · bge-small-zh-v1.5** | `EMBEDDING_PROVIDER=fastembed`，512 维；模型仓库 `Qdrant/bge-small-zh-v1.5`（`model_optimized.onnx`）；CI/单测仍用 `hash` 保证离线稳定 | 决定检索质量与 CI 是否可离线 |
| D3 | 语料 | ✅ **我编写示例语料** | 售后政策 3 篇 + FAQ 40 条 + 商品手册 1 篇，文件头标注「示例语料，M4 替换真实文档」 | 决定完成率基线的可信度 |

补充决策（Spec 已定，无需你选）：**引入 LangGraph**（Spec §2.2 要求）。已做依赖预检（§11），仍需 0.5 天 spike 验证 StateGraph / `AsyncRedisSaver` TTL 与 3.11 运行期行为；若遇到不可绕过的问题，则先落"等价内部图执行器"并写 ADR-0002，M1.5 迁回，**节点契约不变**。

---

## 3. 架构落点（在 M0 骨架上加什么）

```text
POST /chat/stream (SSE)
   │  TraceMiddleware → 限流 → 会话并发锁
   ▼
LangGraph 图（app/agent/graph.py，M0 已定义节点名/超时/裁剪常量）
   router ──qa──► rewrite ──► retrieve ──► answer ──► sse_out
     │                        │(低置信)      ▲
     ├──chitchat──► reject     └──► clarify ─┘
     └──(query/after_sales → M2 工具链，M1 返回"需要订单号/办理能力 M2 上线")
   
retrieve 内部（app/rag/retriever.py 扩展，复用已实现的 classify_confidence / reciprocal_rank_fusion）
   本地LRU+Redis 缓存 → 向量通道(Chroma REST, Top20) + 关键词通道(MySQL ngram, Top20)
   → RRF 融合 Top10 → Reranker Top5 → 阈值三档判定
   
answer 内部
   Prompt Registry(key@version) + <user_input> 注入防御 → 流式生成 → 引用标记解析
   → 出口脱敏（手机号/身份证/地址）→ 无引用则降级兜底
```

**新增模块（全部在 M0 已建好的目录内，不新增顶层目录）**

| 模块 | 职责 | 备注 |
| --- | --- | --- |
| `app/llm/provider.py` | `ChatModel` / `EmbeddingModel` 协议 + `MockProvider` / `OpenAICompatibleProvider` / `Cassette` 录制回放 | D1 的落点 |
| `app/llm/prompts.py` | Prompt Registry 加载 + 注入防御包裹 + 出口脱敏 | 复用 `prompt_registry` 表（M0 已建） |
| `app/rag/chroma.py` | Chroma HTTP 客户端（httpx，不引 chromadb 重依赖） | 用现成 `CHROMA_URL` |
| `app/rag/keyword.py` | `KeywordIndex` 协议 + MySQL ngram 实现 + 内存 BM25（测试/评测用） | |
| `app/rag/indexer.py`（实现） | 文档→分块(500/80)→批量向量化(32)→写 Chroma+`rag_chunk`→`index_version` 流转 | M0 只有规划 |
| `app/rag/cache.py`（扩展） | 两级缓存 + 单飞（singleflight）+ 索引版本联动失效 | 键规范 M0 已定 |
| `app/agent/nodes/*.py` | 7 个节点实现 | M0 只有节点名常量 |
| `app/agent/checkpointer.py`（实现） | `AsyncRedisSaver` + TTL 24h | spike 后定 API |
| `app/session/memory.py` | 滑动窗口（10 轮）+ 超长摘要压缩 | 复用 `sess:`/`ckpt:` 键 |
| `app/api/sse.py`（扩展） | `id:` 序号 + `Last-Event-ID` 续传 + 帧缓冲 | M0 已有事件枚举/格式 |
| `app/services/trace_store.py` | span 落库与调用树组装 | `/traces/{id}` 从 501 转实现 |
| `app/eval/runner.py`（实现） | 断言执行 + 归因 + `eval_run` 落库 + CLI | M0 已有门禁判定 |
| `app/services/ratelimit.py` | 用户级 20rpm + 单会话并发 1（Redis） | §14 |

**数据模型增量（2 个迁移，向前兼容）**

| 迁移 | 表 | 关键点 |
| --- | --- | --- |
| `0002_rag_chunk` | `rag_chunk` | `(index_version, doc_id, chunk_index)` 唯一；`FULLTEXT(content, title) WITH PARSER ngram`；`content_hash` 便于增量重建 |
| `0003_trace_span` | `trace_span` | `trace_id/span_id/parent_span_id/name/start_ms/duration_ms/status/attrs/tokens`；索引 `(trace_id, start_ms)`；90 天清理留 M3 |

**配置增量（`.env.example` 同步 + 单测守护）**

```bash
LLM_PROVIDER=mock            # mock | openai
LLM_MOCK_CASSETTE_DIR=evals/cassettes
EMBEDDING_PROVIDER=hash      # hash | fastembed | api
EMBEDDING_DIM=256
RERANK_PROVIDER=llm          # none | llm | local
RAG_TOP_K_VECTOR=20
RAG_TOP_K_KEYWORD=20
RAG_TOP_K_FUSED=10
RAG_TOP_K_FINAL=5
RAG_RRF_K=60
KEYWORD_BACKEND=mysql        # mysql | memory
SESSION_WINDOW_TURNS=10
SSE_BUFFER_TTL_SEC=300
SSE_BUFFER_MAX_FRAMES=500
TRACE_SPAN_ENABLED=true
```

---

## 4. 工单拆解（15 张，含依赖与估时）

> 分 3 个迭代交付，每个迭代结束都要求 **CI 全绿 + 可演示**。顺序即依赖顺序。

### 迭代 I1（第 1 周）· 检索闭环：语料 → 索引 → 召回 → 引用（离线可测）

| ID | 任务 | 依赖 | 估时 | 验收 |
| --- | --- | --- | --- | --- |
| M1-00 | **LangGraph 兼容 spike**：3.11 下装 `langgraph`+`langgraph-checkpoint-redis`，验证 StateGraph/条件边/AsyncRedisSaver TTL；输出 ADR-0002 | — | 0.5d | 一个 2 节点 demo 图跑通 + checkpoint 可恢复 |
| M1-01 | LLM/Embedding 抽象 + Mock/录制回放 + OpenAI 兼容实现 | M1-00 | 1.5d | 协议单测；同一 prompt 下 mock 输出稳定；录制→回放字节级一致 |
| M1-02 | 示例语料 + `chunker` 实现 + `rag_chunk` 迁移 | M1-01 | 2d | 500/80 切分；元数据齐全；`alembic upgrade/downgrade` 通过 |
| M1-03 | 索引管道：批量向量化(32) → Chroma + MySQL → `index_version` building→active | M1-02 | 2d | 一键重建脚本；`doc_count` 正确；切版本后旧 collection 保留可回滚 |
| M1-04 | 混合召回：向量 Top20 + MySQL ngram Top20 → RRF Top10 → Reranker Top5 → 三档阈值 | M1-03 | 2d | 手算 RRF 用例对齐；Top1<0.55 绝不进生成；检索 P95 ≤ 1.2s |

### 迭代 I2（第 2 周）· 对话闭环：图 + 流式 + 缓存 + 记忆

| ID | 任务 | 依赖 | 估时 | 验收 |
| --- | --- | --- | --- | --- |
| M1-05 | Prompt Registry 读取 + `<user_input>` 注入防御 + 出口脱敏 | M1-01 | 1.5d | 注入红队 10 条全部拒绝跨段指令；手机号/身份证打码单测 |
| M1-06 | 图装配：router/rewrite/retrieve/answer/clarify/reject/sse_out + 条件边 + 调用上限 | M1-04 M1-05 | 3d | 四类意图分流正确；节点超时/失败按 §6.2 降级；checkpoint 重放安全 |
| M1-07 | SSE 流式 + 引用事件 + `id:` 续传（30s 内 `Last-Event-ID` 不重放） | M1-06 | 2d | 首 token P95 ≤800ms（mock）；断线重连帧连续 |
| M1-08 | 会话记忆：滑动窗口 10 轮 + 超长摘要 + 指代改写接线 | M1-06 | 1d | 第 11 轮触发摘要且不超窗；多轮用例改写命中率可测 |
| M1-09 | 两级缓存 + 单飞防击穿 + 索引版本联动失效 | M1-04 | 1.5d | 并发 20 同 query 只回源 1 次；切索引后无旧结果 |

### 迭代 I3（第 3 周）· 质量与可观测：评测、trace、看板、门禁

| ID | 任务 | 依赖 | 估时 | 验收 |
| --- | --- | --- | --- | --- |
| M1-10 | 90 条评测用例（40 典型/15 多跳/15 无答案/20 多轮）+ 执行器 + 失败归因 | M1-07 | 3d | `/eval/run` 返回完成率/引用率/P95/归因分布；门禁可阻断 |
| M1-11 | `trace_span` + span 写入 + `/traces/{id}` 调用树 | M1-06 | 1.5d | 单 trace_id 还原 api→node→rag→llm 各段耗时与 token |
| M1-12 | 指标接线（chat_latency/缓存命中/token/无引用）+ Grafana 看板 v1 | M1-07 | 1d | 看板 4 条核心曲线可看；告警规则引用真实指标 |
| M1-13 | 限流（20rpm/用户）+ 单会话并发 1 + 输入长度/注入防护收口 | M1-06 | 1d | 压测触发 429 AS-4290；并发同会话只有 1 个流 |
| M1-14 | CI 扩展（RAG 集成 job：MySQL+Chroma；评测抽样）+ 文档/ADR | M1-10 | 1.5d | 新增 job 全绿；M0 四 job 不退化 |

**合计 ≈ 23 人日 ≈ 3 周**（单人）。若你希望更快，可把 M1-11（trace 树）与 M1-12（看板）延到 M1.5，核心链路 2 周可交付。

---

## 5. 关键技术方案（决定实现质量的 6 个点）

### 5.1 Provider 抽象与"可离线"

- `ChatModel`：`astream(messages, **kw) -> AsyncIterator[Delta]` + `complete_json(schema)`。
- `MockProvider`：从 `evals/cassettes/*.json` 回放；**未命中即失败**（不允许静默兜底），CI 才能当真门禁。
- `OpenAICompatibleProvider`：httpx 直连 `/chat/completions`（`stream=true`），带超时、重试 1 次、主备模型切换（`LLM_FALLBACK_MODEL`）。
- Embedding 三实现：`hash`（CI，256 维 char-ngram 哈希）、`fastembed`（本地 bge-small-zh）、`api`。
- **理由**：Spec Q2 未定选型，抽象层让选型变成配置，不阻塞 M1。

### 5.2 检索：双通道 + RRF + 重排

- 向量通道走 **Chroma REST**（httpx），不引入 `chromadb` 客户端（重依赖、版本漂移）。
- 关键词通道走 MySQL `FULLTEXT ... WITH PARSER ngram`；`KeywordIndex` 协议 + 内存 BM25 实现供单测/离线评测。
- RRF 用 M0 已实现并测过的 `reciprocal_rank_fusion`（k=60）。
- Reranker 三档：`none`（直接用融合序）/ `llm`（Top10→JSON 打分，超时 800ms 回退融合序）/ `local`（可选 cross-encoder extra）。
- **降级链**：Reranker 超时 → 融合序；向量通道失败 → 关键词单通道；两通道都失败 → 低置信兜底（不编造）。

### 5.3 引用与"不编造"的双保险

1. **生成层**：Prompt 要求每个事实句带 `[[n]]` 标记，n 对应传入片段编号。
2. **校验层**：解析标记 → 映射 `citation_id` → 无标记的答案判为不合规，走兜底话术并记 `answer_without_citation_total`（新指标），该条用例在评测中直接判失败。
3. **阈值层**：Top1 < 0.55 时**根本不调用生成**（省成本 + 杜绝幻觉）。

流式下无法撤回已发 token，因此顺序是：先检索判定 → 再开流 → 边流边发 `citation` 事件（标记完成即发）→ `done` 前做一次整体校验，不合规则发 `error` 并以兜底话术收尾。

### 5.4 流式续传（Spec §15 明确要求）

- 每个 SSE frame 带 `id: <seq>`；帧写入 Redis `sse:{session_id}`（list，cap 500，TTL 300s）。
- 客户端重连带 `Last-Event-ID`；服务端回放 `seq > last` 的帧后继续实时流。
- 限制：超过 300s 或丢弃帧数超上限则返回"需重新提问"（新建 turn），避免无限缓冲。

### 5.5 缓存与单飞

- L1 进程内 TTL 字典（60s，按索引版本失效）；L2 Redis（`rag:{ver}:{hash}`，300s）。
- 单飞：进程内 `asyncio.Lock` per key + 跨进程 `SET NX PX 5000`；未抢到锁的等待结果（50ms 轮询，最多 10 次），超时返回降级值。
- 索引激活时按 `rag:` 前缀扫描删除（用 `SCAN` 而非 `KEYS`）。

### 5.6 评测与归因

- 用例 JSON（M0 已有 11 条，扩到 90 条），字段沿用现有 schema。
- 断言维度按类别：`intent` 命中、`keywords` 包含、`must_cite`/`min_citations`、`must_refuse`/`must_fallback`、多轮的 `must_resolve_reference`。
- 归因：记录失败发生在 `router` / `retrieve` / `rerank` / `answer` / `citation` 哪一段（用 trace span 推断，不靠猜）。
- 门禁复用 M0 的 `evaluate_gate`（完成率/引用率/P95/非法迁移）。

---

## 6. 测试与 CI 变更

| 层 | 内容 | 是否进 CI |
| --- | --- | --- |
| 单测 | chunker、BM25、RRF、阈值、prompt 注入/脱敏、引用解析、SSE 帧与续传、缓存单飞、路由规则兜底、评测断言 | ✅ 每个 PR |
| 集成 | MySQL ngram 检索、`rag_chunk` 迁移、Chroma 写入/查询（compose 起 MySQL + Chroma） | ✅ 新增 `rag-integration` job |
| e2e | `/chat/stream` 三条路径（正常带引用 / 低置信兜底 / 闲聊拒绝），mock provider | ✅ quality job |
| 评测 | mock 模式 20 条抽样作为"管道门禁"；真实模型全量 90 条在 staging | 抽样 ✅ / 全量 ⏭ |

新增 CI job：`rag-integration`（MySQL + Chroma 服务容器）与 `eval-smoke`（mock 20 条），M0 的 4 个 job 保持不动。

---

## 7. 风险与应对

| 风险 | 等级 | 应对 |
| --- | --- | --- |
| 无真实 LLM → 质量门禁无法验收 | 高 | 抽象层 + mock 基线先行；门禁分"管道门禁（CI）"与"质量门禁（staging）"两档，写进验证记录 |
| LangGraph API 变动 / checkpoint 与 Redis 兼容 | 中 | M1-00 半天 spike 先验证；失败则内部图执行器 + ADR，接口不变 |
| MySQL ngram 对中文短 query 召回差（单字被丢） | 中 | `ngram_token_size=2` 默认；测评对比后可选 `jieba` 分词写自定义分词列；RRF 向量通道兜底 |
| Chroma REST 版本差异（0.5.x heartbeat/路径） | 中 | 客户端只依赖 `/api/v1/...` 四个端点；compose 固定镜像版本；集成测试覆盖 |
| 流式 + 引用 + 续传三者交互复杂 | 中 | 先做"非流式完整答案 + 引用"跑通评测，再接流式（I2 内两步走） |
| 90 条用例编写耗时且主观 | 中 | 用模板批量起草 + 我逐条复核；无答案类必须写明"必须拒绝"的判据 |
| 依赖膨胀拖慢 CI | 低 | fastembed 进 optional extra；CI 默认 hash；镜像仍只装必需依赖 |

---

## 8. 交付物清单

```text
app/llm/{provider,cassette,prompts}.py         新增
app/rag/{chroma,keyword}.py                    新增；indexer/chunker 实现
app/agent/nodes/{router,rewrite,retrieve,answer,clarify,reject,sse_out}.py  新增
app/agent/{graph,checkpointer}.py              实现（M0 已留契约）
app/session/memory.py                          新增
app/api/sse.py                                 扩展（seq/续传）
app/services/{trace_store,ratelimit}.py        新增
app/eval/{runner,cli}.py                       实现
app/migrations/versions/0002_rag_chunk.py      新增
app/migrations/versions/0003_trace_span.py     新增
app/common/{config,metrics}.py                 扩展
seeds/{prompts.yaml,corpus/*.md}               新增（示例语料标注）
evals/cases/*.json                             11 → 90 条
evals/cassettes/*.json                         mock 回放
deploy/grafana/dashboards/rag-v1.json          新增
docs/m1/{01-plan.md,02-tickets.md,03-verification.md}  + ADR-0002
.github/workflows/ci.yml                       +2 job
```

---

## 9. 排期与节奏

| 周 | 内容 | 周末状态 |
| --- | --- | --- |
| W1 | I1：spike → provider → 语料/分块 → 索引 → 混合召回 | 离线可用：给一句话能返回 Top5 片段与分数（CLI 演示） |
| W2 | I2：Prompt/防御 → 图 → SSE 流式 → 记忆 → 缓存 | 端到端可用：`/chat/stream` 返回带引用回答（mock 或真实模型） |
| W3 | I3：评测 90 条 → trace 树 → 指标看板 → 限流 → CI | M1 出口评审，门禁数据留档 |

---

## 10. 依赖预检与已验证事实（2026-10-27 实测）

| 项 | 结果 | 处置 |
| --- | --- | --- |
| `langgraph` | ✅ 1.2.18 可安装 | 与 `langgraph-checkpoint-redis==0.5.2` 解析无冲突（连带 `langgraph-checkpoint 4.3.1`、`redisvl 0.28.0`、`numpy 2.4.x`） |
| `langgraph-checkpoint-redis` | ✅ 0.5.2 | spike 验证 `AsyncRedisSaver` 的 TTL/`asetup()` 实际签名 |
| `fastembed` | ✅ 0.9.1 可安装 | 模型仓库 `Qdrant/bge-small-zh-v1.5` 含 `model_optimized.onnx`，512 维 |
| HuggingFace 直连 | ❌ 超时（本机网络） | **必须设置 `HF_ENDPOINT=https://hf-mirror.com`**（实测 200）；写入 `.env.example` 并在 README 说明 |
| 语料/评测 | 待编写 | 90 条用例由模板起草后逐条复核 |

**安全与隐私**（接真实网关时必须显式关闭第三方追踪）：

- `LANGCHAIN_TRACING_V2=false`、不设 `LANGCHAIN_API_KEY`（LangGraph 生态默认会尝试上报）；
- LLM key 只走 `LLM_API_KEY_FILE`（建议 `.secrets/llm_key.txt`），`.gitignore` 需补 `.secrets/`；
- 请求/响应日志只记 `params_hash` 与 token 数，不落原文（Spec §14 脱敏）。

---

## 11. 开工所需信息（只差这一组）

要把 D1 从"协议实现"落成"真实联调"，需要你提供 3 个值（**key 不要贴在对话里**）：

1. `LLM_BASE_URL`：网关的 OpenAI 兼容根地址，例如 `https://your-gateway/v1`；
2. `LLM_MODEL` 与 `LLM_FALLBACK_MODEL`：主模型名、备模型名（只给主模型也行，备模型可留空）；
3. key 的**文件路径**：把 key 写进 `D:\AI\e-co\.secrets\llm_key.txt`（我会先建目录并加 gitignore），然后告诉我"key 已放好"。

拿到后我立即按 I1 → I2 → I3 开工，每完成一个迭代交付一次「可运行 + CI 绿」，并同步更新：
`docs/m1/02-tickets.md`（状态）、`docs/m1/03-verification.md`（实测数据）、ADR-0002（LangGraph spike 结论）。

> 若你希望**不等 key** 就先动工：回一句「先按 mock 开始」，我立刻做 I1 中与模型无关的部分
> （语料、分块、索引管道、关键词通道、RRF 接线），把真实模型留到 I2 联调时切换——代码零改动。
