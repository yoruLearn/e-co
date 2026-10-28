# M1 验证记录 · 问答链路

> 逐迭代留痕：做了什么、跑了什么、数字是多少、哪些是"假绿"必须排除。

## 迭代 I3 · 第 9 天（2026-10-28）：指标看板 / 限流 / CI 评测抽样

| # | 命令 | 结果 |
| --- | --- | --- |
| 1 | `ruff check .` / `ruff format --check .` | ✅ 150 文件 |
| 2 | `mypy app`（strict） | ✅ 88 source files |
| 3 | `pytest -q --cov=app` | ✅ **506 passed, 10 skipped，83.29%** |
| 4 | `pytest tests/unit/test_ratelimit.py tests/integration/test_chat_stream.py` | ✅ 20 例（含 429/会话互斥/延迟指标） |
| 5 | `python -m app.eval.cli ... --json`（CI 同口径） | ✅ 5 条抽样，`total=5`，JSON 可解析 |

### M1-12 指标看板

| 指标 | 落点 | 说明 |
| --- | --- | --- |
| `chat_latency_seconds` | chat 路由 finally | 端到端耗时（含 SSE 全流程） |
| `chat_first_token_seconds{mode}` | chat 路由首个 delta | `generated` / `fallback` 分开；**不把推理模型的 2s 首字藏进平均值** |
| `rag_cache_hit_ratio` | `TwoLevelCache._publish_hit_ratio` | L1+L2 命中 / 总请求 |
| `llm_tokens_total{node}` | provider（已有，M1 生效） | 成本按节点归因 |
| `answer_without_citation_total` | answer 节点 | 无引用拦截计数（应为 0） |

看板：`deploy/grafana/provisioning/dashboards/m1-rag.json`（P95/P50、首片段 P95、5xx、缓存命中、无引用、token 速率、请求/错误）；
datasource 增加 `uid: prometheus`，由 provisioning 自动加载。

### M1-13 限流与会话互斥

| 场景 | 行为 | 验证 |
| --- | --- | --- |
| 用户 >20 次/分钟 | 429 `AS-4290`，detail 带 `retry_after_seconds` | 单测（第 3 次拒绝）+ 接口测试 |
| 同一 session 并发 | 第二个请求 429（"正在处理上一条消息"） | 预置锁 → 429 |
| Redis 故障 | **放行**并告警（保护性措施不阻断业务） | 故障注入单测 |
| 正常完成 | 流结束释放锁（finally） | 互斥测试：释放后可再次获取 |

### M1-14 CI 评测抽样

新增 `eval-sampling` job：`LLM_PROVIDER=mock` + `EMBEDDING_PROVIDER=hash`，跑 `--offline --categories no_answer --limit 5 --json`，
断言 `total==5` 且报告含 `attribution/gate`，上传 artifact。**不调用真实模型**，不会产生费用。

### 本轮修掉的问题

| # | 问题 | 修复 |
| --- | --- | --- |
| 1 | CLI 的 `[eval] cases=...` 打到 stdout，`--json` 输出被污染 | 进度改 stderr，stdout 只留 JSON（CI 解析才可靠） |
| 2 | `.env.example` 先加 `RATE_LIMIT_PER_MIN`（Settings 里叫 `USER_RATE_LIMIT_PER_MIN`） | 用真实字段名并去重，`test_config` 恢复绿色 |
| 3 | chat 的指标/解锁 `finally` 一开始挂在 except 分支上 | 移到 try/finally 层，指标测试从 0 → 有样本 |

### M1 出口门禁现状

| 项 | 目标 | 实测 | 结论 |
| --- | --- | --- | --- |
| 完成率 | ≥90% | 94.44%（85/90） | ✅ |
| 引用正确率 | ≥95% | 100% | ✅ |
| 无引用直出 | 0 | 0（指标可观测） | ✅ |
| 首片段 P95 | 缓存命中 ≤800ms | 已分 mode 采集；未做压测样本 | ⚠️ 需 staging 压测 |
| 限流/会话并发 | 生效 | 429 + 互斥 | ✅ |
| 越界兜底 | 不硬答 | 5/15 仍作答 | ⚠️ M2 可答性分类器 |

---

## 迭代 I3 · 第 8 天（2026-10-28）：评测调优 + M1-11 调用树

| # | 命令 | 结果 |
| --- | --- | --- |
| 1 | `ruff check .` / `ruff format --check .` | ✅ 148 文件 |
| 2 | `mypy app`（strict） | ✅ 87 source files |
| 3 | `pytest -q --cov=app` | ✅ **498 passed, 10 skipped，83.13%** |
| 4 | `pytest tests/integration/test_traces_api.py tests/integration/test_trace_spans.py` | ✅ 7 例（span 落库/接口/角色/404/图节点全覆盖） |
| 5 | **`python -m app.eval.cli run --offline`（真实 DeepSeek + bge，90 条）** | ✅ **85/90（94.44%）**，引用 100%，P95 4647ms，门禁通过 |

### 评测调优过程（不是一次性写对）

| 轮次 | 完成率 | 引用正确率 | 主要失败 | 动作 |
| --- | --- | --- | --- | --- |
| 首轮 | 81.11% | 96.23% | rewrite 6 / retrieve 5 / router 4 / answer 2 | 定位：多轮改写未变化、rag 意图误判、部分用例期望本身不合理 |
| 第 2 轮 | 92.22% | 96.23% | retrieve 4 / router 2 / answer 1 | router/rewrite prompt 加 few-shot；改写增加**确定性兜底**；修正 query 路径不经过 rewrite 的错误期望 |
| 第 3 轮 | 92.22% | 96.23% | answer 2 / retrieve 5 | 强化 answer prompt 的引用硬约束（"无编号视为无效"） |
| 第 4 轮 | **94.44%** | **100%** | retrieve 5 | 仅剩 5 条越界问题未兜底（见下） |

### 本轮的真实修复（代码而非改用例）

1. **router prompt**：补充"问政策 vs 发起办理 vs 指代查订单"的边界与 4 条 few-shot；
2. **rewrite prompt**：加 few-shot，要求"补全指代、必须问号结尾"；
3. **改写确定性兜底**（`rule_rewrite`）：模型原样返回或只回标点时，用"上一轮话题；本轮问题"兜底，避免多轮改写静默失效；
4. **answer prompt**：明确"每个事实句必须带 `[[n]]`，无编号视为无效"，并给示例；
5. **M1-11**：`trace_span` 迁移 + 每节点 span + `GET /traces/{id}`。

### 一次失败的尝试（保留记录，避免下次重犯）

为压掉"越界问题硬答"，曾加"查询 2-gram 覆盖率 < 0.45 → 强制兜底"。
实测覆盖率**不能区分**域内/域外（域内最低 0、域外 0.11-0.38，分布重叠），
导致 13 条域内用例被误判兜底、引用正确率跌到 75%。已回滚，阈值不再参与判定。
正确方向是 M2 的"可答性分类器/语料覆盖索引"，而不是拍一个字符覆盖率阈值。

### 本轮未解决（诚实记录）

| 项 | 现状 | 计划 |
| --- | --- | --- |
| 5 条越界问题（海外仓/延保/上门安装/会员积分/线下门店） | 检索给了 0.55-0.61 的"带保留"分档并作答，未走兜底 | M2 增加可答性判定（LLM 分类或覆盖索引），不作为 M1 门禁 |
| `eval_run` 落库 | 本地无 MySQL，走"落库失败只告警"分支；CI 迁移 job 验证表结构 | staging 验证 |
| 首 token P95 | 未接指标（M1-12） | 下一轮 |

---

## 迭代 I3 · 第 7 天（2026-10-27）：M1-10 评测执行器 + 90 条用例（首次真实评测）

| # | 命令 | 结果 |
| --- | --- | --- |
| 1 | `ruff check .` / `ruff format --check .` | ✅ 144 文件 |
| 2 | `mypy app`（strict） | ✅ 86 source files |
| 3 | `pytest -q --cov=app` | ✅ **491 passed, 10 skipped，覆盖率 83.62%** |
| 4 | `pytest tests/unit/test_eval_*.py tests/integration/test_eval_api.py` | ✅ 断言/执行器/接口共 40 例 |
| 5 | **`python -m app.eval.cli run --offline`（真实 DeepSeek + bge 向量，90 条）** | ❌ **门禁未过**：73/90（81.11%），引用 96.23%，P95 4674ms，耗时 215s |

### 真实评测明细（离线：内存索引 + 真实模型 + 真实 embedding）

| 指标 | 数值 | 门禁 | 结论 |
| --- | --- | --- | --- |
| 完成率 | **81.11%（73/90）** | ≥85%（Spec）/ 90%（代码默认） | ❌ |
| 引用正确率 | **96.23%** | ≥95% | ✅ |
| P95 | **4674ms** | ≤1500ms（缓存命中）/ 单轮生成另计 | ❌（推理模型 + 无缓存，见说明） |
| 失败归因 | `rewrite 6`、`retrieve 5`、`router 4`、`answer 2` | — | 主要矛盾在多轮改写与多跳召回 |

典型失败（节选）：

- `multi-turn-001`：期望 `query`，实际 `qa`（"那这一单呢"缺订单上下文时被归为知识问答）；
- `multi-turn-002/009/013/014/019`：改写未发生变化（`rewritten_query == 原句`）；
- `multi-turn-004`：答案未携带 `[[n]]` 且未改写；
- 多跳用例（`multi-hop-*`）：`min_citations=2` 未满足，Top-K 只命中 1 条。

> 这是 M1 第一次**用真实数据说话**：81.11% 不是"差一点"，而是明确暴露了
> rewrite 节点的提示词与 router 的指代类样本不足；修复计划见 `docs/m1/04-handoff.md` §3。

### 本轮交付

| 能力 | 落点 |
| --- | --- |
| 90 条用例（40/15/15/20） | `evals/cases/*.json` |
| 断言 + 归因 | `app/eval/assertions.py`（意图/关键词/引用/兜底/拒绝/改写六维） |
| 执行器 | `app/eval/executor.py`（P95 最近秩、引用正确率、归因汇总、门禁） |
| 图执行 | `app/eval/graph_executor.py`（每条用例独立 thread，异常归因 graph） |
| 接口 | `POST /eval/run`（ops/admin；落 `eval_run` 表，落库失败只告警） |
| CLI | `python -m app.eval.cli run [--offline] [--limit N] [--categories ...] [--json]` |
| 离线运行时 | `app/eval/offline.py`（内存向量索引 + 内存 BM25，无需 MySQL/Chroma） |

### 本轮修掉的问题

| # | 问题 | 修复 |
| --- | --- | --- |
| 1 | `citations` 存 pydantic 模型触发 LangGraph serde 警告（未来版本拒绝反序列化） | 状态里改存 `dict`，节点输出 `model_dump()` |
| 2 | 评测用 `SimpleNamespace` 冒充 runtime，lifespan 关闭时 `close()` 报错 | 测试改用带 `graph/checkpointer/close` 的 `FakeRuntime` |
| 3 | 缓存/单飞改造后 `RetrievalCache` 协议缺失导致 mypy 报未覆盖错误码 | 定义协议并去掉 `type: ignore` |

### 本轮的"未验证"

| 项 | 原因 |
| --- | --- |
| staging 真实栈（MySQL + Chroma + 正式语料）评测 | 本机 Docker Hub 不可达；CI 只验证管道连通性，不跑真实模型 |
| 首 token P95 | 推理模型首正文 ~2.2s；需 M1-12 指标与缓存命中率一起看 |
| M1-11/12/13/14 | 未开始（见 handoff P1） |

---

## 迭代 I2 收尾 · 第 6 天（2026-10-27）：M1-07/08/09

| # | 命令 | 结果 |
| --- | --- | --- |
| 1 | `ruff check .` / `ruff format --check .` | ✅ 135 文件 |
| 2 | `mypy app`（strict） | ✅ 80 source files |
| 3 | `pytest -q --cov=app` | ✅ **463 passed, 10 skipped，覆盖率 85.43%** |
| 4 | `pytest tests/integration/test_chat_stream.py` | ✅ 12 例（含续传、多轮改写） |
| 5 | `pytest tests/unit/test_cache.py tests/unit/test_session_store.py` | ✅ 19 例 |

### M1-07 断线续传

| 项 | 实现 | 验证 |
| --- | --- | --- |
| 帧 id | Redis `INCR sse:seq:{sid}`，会话级递增，跨副本唯一 | 帧 id 连续无跳号 |
| 帧缓冲 | `sse:frames:{sid}` list，cap 500 / TTL 300s；Redis 挂掉降级进程内 | 容量/前缀测试 |
| 续传 | `Last-Event-ID: n` → 只回放 id>n 的帧，**不重新生成答案** | 测试断言 `stream_calls` 不变、回放 id 序列完全一致 |
| 过期 | 无帧可回放 → `error`(AS-4001) + `done` | 测试覆盖 |
| 非法头 | 非整数/负数 → 400 `AS-4001` | 测试覆盖 |

### M1-08 会话记忆与改写

- 窗口 10 轮；被挤出的轮次先进 `archive`（上限 1500 字），每挤出 2 轮才调用一次摘要模型；
- 摘要成功后清空 archive，拼进 `summary`（≤600 字）；失败保留原文，下轮重试；
- 历史注入顺序：`（历史摘要）` → `（更早的对话原文）` → 最近窗口；
- 多轮用例：带历史时同时调用 router + rewrite，检索用改写后的 query（有断言）。

### M1-09 缓存与单飞

| 项 | 实现 | 验证 |
| --- | --- | --- |
| 两级缓存 | L1 进程内 60s；L2 Redis 300s（`cache:rag:{ver}:{hash}`） | L2 命中、前缀失效测试 |
| 单飞 | per-key asyncio.Lock + 双检；等锁超时返回缓存/降级值 | 并发 8 请求 → factory 只跑 1 次 |
| 版本联动 | 检索结果按索引版本分 key；版本切换按 `rag:{old}:` 前缀失效 | 跨版本不命中测试 |
| 降级 | Redis 故障时只读 L1，业务不失败 | 故障注入测试 |

### 本轮修掉的问题

| # | 问题 | 修复 |
| --- | --- | --- |
| 1 | 摘要触发条件写成"总轮数 > 窗口 2 倍"，但每轮 append 都会裁剪，永不触发 | 改为"挤出即归档，每挤出 2 轮压缩一次" |
| 2 | `redis-py` 部分方法返回 `Awaitable[T] \| T`，直接 await 触发 mypy 报错 | `_maybe_await` 统一处理 |
| 3 | 缓存对象用 `object` 类型 + `type: ignore`，mypy 报未覆盖错误码 | 定义 `RetrievalCache` 协议，去掉 ignore |

### 本轮的"未验证"

| 项 | 原因 | 何时补 |
| --- | --- | --- |
| 首 token P95 / 端到端 P95 | 未接真实 Chroma+embedding 计时，且真实模型推理首字 ~2.2s | M1-12 指标 + 压测 |
| `rag_cache_hit_ratio` 指标 | 缓存命中统计未接 Prometheus gauge | M1-12 |
| 摘要对长会话质量 | 需评测集多轮用例 | M1-10 |

---

## 迭代 I2 · 第 5 天（2026-10-27）：M1-06 图装配 + M1-07 流式 SSE

| # | 命令 | 结果 |
| --- | --- | --- |
| 1 | `ruff check .` / `ruff format --check .` | ✅ 129 文件 |
| 2 | `mypy app`（strict） | ✅ 76 source files |
| 3 | `pytest -q --cov=app` | ✅ **439 passed, 10 skipped，覆盖率 85.30%** |
| 4 | `pytest tests/integration/test_chat_stream.py` | ✅ 8 例（事件顺序/引用/脱敏/无引用拦截/四类意图） |
| 5 | `pytest tests/unit/test_agent_nodes.py tests/unit/test_streaming_redactor.py` | ✅ 21 例 |

### 图结构（M1 问答链路）

```text
START → router ─qa→ rewrite → retrieve ─高置信→ answer → sse_out → END
            │                       └低置信→ clarify → sse_out
            ├query/after_sales→ pending_capability（M2 接工具）→ sse_out
            └chitchat→ reject → sse_out
```

| 节点 | 行为 | 失败策略 |
| --- | --- | --- |
| `router` | LLM 结构化 JSON（2s 超时）→ 解析失败/超时用**规则兜底** | 记 `degraded=router:*`，继续 |
| `rewrite` | 有历史才改写成独立 query（3s） | 回退原句 |
| `retrieve` | 混合召回 + 三档判定（1.2s+） | 异常 → band=fallback |
| `answer` | 流式生成 + `[[n]]` 引用事件 + 句子级脱敏 + 无引用拦截 | LLM 异常 → error 事件 + 兜底话术 |
| `clarify`/`reject`/`pending` | 确定性话术（不调模型，零成本、可评测） | — |

### SSE 协议落地

| 项 | 结果 |
| --- | --- |
| 事件 | `status` / `delta` / `citation` / `clarify` / `error` / `done`（`confirm`/`tool` 留 M2） |
| 帧序号 | 每帧 `id: <seq>`，测试断言连续无跳号 |
| 引用 | 模型输出 `[[1]]` 时**实时**发 `citation`（含 doc_id/title/section/snippet/score） |
| 脱敏 | 句子级切分后打码：手机号 `138****5678`、身份证、银行卡、邮箱；**号码跨 chunk 不会泄露半截** |
| 无引用拦截 | 有检索结果但答案无 `[[n]]` → 替换为兜底话术 + `clarify` 事件 + `answer_without_citation_total` 指标 |
| 记忆 | 回答完成后写 `sess:{id}`（窗口 10 轮）；Redis 不可用时降级为无记忆 |

### 本轮修掉的问题

| # | 问题 | 修复 |
| --- | --- | --- |
| 1 | `PromptRegistry` 的 DB 源在无数据库时抛 `OperationalError`，把整图打断 | `get()` 对每个 source 做异常隔离 → 记 `prompt.source.failed` 并回落到 seed |
| 2 | LangGraph serde 警告 `Intent`/`AfterSalesStatus` 未注册（未来版本会拒绝反序列化） | 状态里 `intent`/`status` 改存**字符串**，枚举只在边界转换 |
| 3 | SSE 测试消息选错（"我的退款到哪了"是 query 意图，走 pending 分支） | 用 qa 消息覆盖检索兜底路径，另加 pending 分支单测 |

### 本轮的"未验证"（不许算绿）

| 项 | 原因 | 何时补 |
| --- | --- | --- |
| `Last-Event-ID` 断线续传 | 帧缓冲与回放未实现（I2 收尾） | 下一轮 |
| 首 token P95 ≤800ms | 真实网关推理模型首正文 ~2.2s，仅缓存命中可达成；且未接缓存 | M1-09 后压测 |
| 真实模型下的四类意图准确率 | 需 90 条评测集 | M1-10 |

---

## 迭代 I1/I2 交界 · 第 4 天（2026-10-27）：M1-00 spike + M1-05 Prompt 层

| # | 命令 | 结果 |
| --- | --- | --- |
| 1 | `ruff check .` / `ruff format --check .` | ✅ 120 文件 |
| 2 | `mypy app`（strict） | ✅ 71 source files |
| 3 | `pytest -q --cov=app` | ✅ **410 passed, 10 skipped，覆盖率 85.12%** |
| 4 | `pytest tests/unit/test_checkpointer.py` | ✅ 9 passed（图机制 + 策略 + 后端选择） |
| 5 | `pytest tests/unit/test_prompt_*.py` | ✅ 24 passed（防御/脱敏/Registry/基线守卫） |
| 6 | CI「RAG 端到端」job | ✅ 新增 MySQL+Chroma+**Redis** 三服务；含 `redis checkpoint` 步骤 |

### M1-00 spike 结论（详见 [ADR-0002](../adr/0002-langgraph-and-checkpointer.md)）

| 项 | 实测 |
| --- | --- |
| langgraph / checkpoint-redis | 1.2.18 / 0.5.2，解析无冲突 |
| `AsyncRedisSaver(ttl=...)` | **TTL 单位是分钟**（写成秒会变 1440 小时）；`refresh_on_read` 可配 |
| 构造阶段 | 不连接 Redis，`redis_url` 指向不存在的端口也能构造成功 |
| `asetup()` | 异步、必须在首次使用前 await |
| 跨实例恢复 | CI 用两个独立 saver 验证同一 `thread_id` 能读回 state |

### M1-05 Prompt 层

| 能力 | 实现 | 测试 |
| --- | --- | --- |
| Registry | `DbPromptSource`（只认 published）→ `SeedPromptSource`（`seeds/prompts.yaml`）回落；进程内 TTL 缓存；`$var` 模板（避免与 JSON 花括号冲突） | 12 例 |
| 注入防御 | `<user_input>` 包裹 + 闭合标签转义（大小写不敏感）+ 控制字符清洗 + 4000 字截断 + 系统段安全声明（幂等追加） | 6 例 |
| 出口脱敏 | 手机号 / 身份证 / 银行卡 / 邮箱打码，返回命中统计；金额、订单号、年份不误伤 | 6 例 |
| 基线守卫 | `prompts.yaml` 必须含 router 四意图、answer_rag 的 `[[1]]` 引用与"不编造"约束 | 3 例 |

### 本轮修掉的问题

| # | 问题 | 修复 |
| --- | --- | --- |
| 1 | 自定义 `PromptRegistry` 与 ORM 模型 `PromptRegistry` 同名，运行时会被覆盖（查询走错类） | ORM 导入别名 `PromptRegistryRow` |
| 2 | `str.format` 与 prompt 里的 JSON 花括号冲突 | 改用 `string.Template`（`$question`） |
| 3 | `async def` 里直接读文件触发 ASYNC240 | 抽出同步 `load_prompt_seeds()` |
| 4 | 脱敏计数用 `subn` 返回 `Any` | 改用 `findall` 计数 + `sub` 替换 |
| 5 | **CI 的 Redis checkpointer 失败**：`RedisSaver` 依赖 RedisJSON + RediSearch，`redis:7-alpine` 不满足 | CI 与 compose 改用 `redis/redis-stack-server:7.4.0-v0`；集成测试改为轮询探活 + `REQUIRE_REDIS=1` 时失败而非 skip（防假绿） |

### 本轮的"未验证"（不许算绿）

| 项 | 原因 | 何时补 |
| --- | --- | --- |
| 本地 Redis checkpointer | 本机 Docker Hub 不可达，拉不到 redis 镜像 | 已由 CI 真实 Redis 覆盖 |
| Prompt 发布/回滚接口 | 属 M3（`/admin/prompts/{key}/publish` + 评测门禁 + 审计） | M3 |
| 提示词对模型的实际效果 | 需接入图与评测集 | M1-06/M1-10 |

---

## 迭代 I1 · 第 3 天（2026-10-27）：M1-03/04 索引管道 + 混合召回

| # | 命令 | 结果 |
| --- | --- | --- |
| 1 | `ruff check .` / `ruff format --check .` | ✅ 109 文件 |
| 2 | `mypy app`（strict） | ✅ 67 source files |
| 3 | `pytest -q --cov=app` | ✅ **377 passed, 9 skipped，覆盖率 86.12%** |
| 4 | `pytest tests/integration/test_retrieval_quality.py`（真实 embedding） | ✅ 6 passed（16.5s） |
| 5 | `python -m app.rag.cli query "退款多久到账" --offline` | ✅ `band=answer top1=0.8142`，Top1=`售后政策总则 / 退款时效` |

### 本轮实现

| 模块 | 内容 |
| --- | --- |
| `app/rag/chroma.py` | Chroma **REST** 客户端（create/upsert/query/delete/heartbeat）+ 测试用 `InMemoryVectorIndex`；不引 chromadb 客户端 |
| `app/rag/keyword.py` | 关键词通道协议 + `MysqlKeywordIndex`（ngram MATCH）+ `MemoryBM25KeywordIndex`（单测/离线）+ 分数归一化 |
| `app/rag/pipeline.py` | `HybridRetriever`：双通道 → RRF Top10 → Rerank Top5 → 三档判定；降级链（向量挂→关键词；全挂→兜底）；`NoopReranker` / `LlmReranker` |
| `app/rag/indexer.py` | 文档 → 分块 → 32/批向量化 → Chroma upsert → 覆盖写 `rag_chunk` → `index_version` building/active/deprecated；`activate()` 支持回切 |
| `app/rag/cli.py` | `rebuild` / `query --offline` / `query --online` / `versions`，支持 `--json` |

### 真实 embedding 检索抽样（bge-small-zh，5 条问句）

| 问句 | Top2 覆盖 | 判定 |
| --- | --- | --- |
| 退款多久能到账 | 退款、工作日 | 通过 |
| 七天无理由从哪天开始算 | 七、签收 | 通过 |
| 退货运费谁承担 | 运费 | 通过 |
| 发票可以重开吗 | 发票 | 通过 |
| 耳机保修多久 | 保修 | 通过 |

> 这是 **抽样冒烟**，不是完成率；完整 90 条评测在 M1-10。

### 本轮修掉的问题

| # | 问题 | 修复 |
| --- | --- | --- |
| 1 | RRF 名次靠前但向量分更低的资料排在上面，导致三档判定用的 top1 与最佳证据不一致 | `NoopReranker` 改为"RRF 决定候选、向量分决定顺序"，并补回归用例 |
| 2 | `logger.info(..., extra={"name": ...})` 触发 `KeyError: Attempt to overwrite 'name'` | 改用 `log_event`（key 名不与 LogRecord 保留字段冲突） |
| 3 | BM25 的 `normalize_for_keyword` 未去尾部空格/未折叠空格 | 统一 `strip + 折叠`，补断言 |
| 4 | `EmptyKeywordIndex` 的空实现参数触发 ARG002 | 每个参数行显式 noqa 并注明"保持协议签名" |

### 本轮的"未验证"（不许算绿）

| 项 | 原因 | 何时补 |
| --- | --- | --- |
| 检索 P95 ≤1.2s | 未在真实 Chroma + 真实 embedding 下计时 | I2 接缓存后压测 |
| 本地 Docker 起 MySQL/Chroma | 本机 Docker Hub 不可达（`registry-1.docker.io:443` 超时），镜像拉不下来；**不是代码问题** | 改由 CI 覆盖（见下） |

### 真实栈验证：交给 CI（run #9）

新增 job `RAG 端到端（MySQL 8 + Chroma）`：GitHub Actions 起 `mysql:8.0` + `chromadb/chroma:0.5.20`
服务容器 → `alembic upgrade head` → `scripts/rag_smoke.py`，断言：

| 断言 | 结果 |
| --- | --- |
| 迁移建出 `rag_chunk` 且 `ft_rag_chunk`（ngram FULLTEXT）存在 | ✅ |
| 语料分块数 == `rag_chunk` 行数 | ✅ |
| `index_version` 变为 active | ✅ |
| 在线混合召回有命中且 `degraded == []` | ✅ |
| 关键词单通道（`collection_id=None`）有命中 → ngram 生效 | ✅ |

> 结论：M1-03/04 的**管道连通性**已由真实 MySQL 8 + Chroma 验证；检索**质量**由本地
> fastembed 抽样（上文 6 例）与 M1-10 的 90 条评测负责，两者不混为一谈。

---

## 迭代 I1 · 第 2 天（2026-10-27）：M1-02 语料 + 分块 + 迁移

| # | 命令 | 结果 |
| --- | --- | --- |
| 1 | `ruff check .` / `ruff format --check .` | ✅ 101 文件 |
| 2 | `mypy app`（strict） | ✅ 62 source files |
| 3 | `pytest -q --cov=app` | ✅ **341 passed, 3 skipped，覆盖率 89.75%** |
| 4 | `alembic upgrade head --sql` | ✅ 生成 9 张 M0 表 + `rag_chunk`，含 `FULLTEXT ... WITH PARSER ngram` |
| 5 | `pytest tests/unit/test_chunker.py` | ✅ 19 例（含语料守卫：FAQ 恰好 40 条、块数 ≥60、每块 ≤500 字） |

### 示例语料（`seeds/corpus/`）

| 文件 | 内容 | 分块 |
| --- | --- | --- |
| `售后政策总则.md` | 适用范围、7 天无理由、15 天质量问题、退款时效、运费、三包 | 按 6 个 `##` 节切分 |
| `退换货细则.md` | 不支持无理由的商品、拆封判定、赠品/发票、取件、换货缺货、部分退 | 按 7 个 `##` 节切分 |
| `退款与发票规则.md` | 退款路径、部分退款与券、价保、发票红冲、退款失败 | 按 5 个 `##` 节切分 |
| `售后常见问题.md` | 40 条高频问答（Q01–Q40），分组标题 | 40 个 `###` 块，逐条可检索 |
| `商品手册-蓝牙耳机Pro.md` | 参数、包装、保修、5 条常见问题、注意事项 | 按节 + 长节滑窗 |

> 语料文件均标注「示例语料，M4 前替换为真实文档」，避免被误当法务文本。

### 分块实现要点

- 标题层级：`##`/`###` 生成 `售后政策总则 > 退款时效` 这样的节路径；`#` 作为文档标题不进正文；
- 500 字硬上限 + 80 字重叠：重叠只在「加上后不超 500」时保留，宁可放弃重叠也不产生超长块；
- 句子优先：按 `。！？；` 切句，尽量避免把一句话拆两半；超长单句（长表格）才硬切；
- 元数据：`doc_id/title/section/version/index/chars` 同时喂 Chroma metadata 与 `rag_chunk`。

### 迁移 0002

```sql
CREATE TABLE rag_chunk (... uk_ver_doc_chunk (index_version, doc_id, chunk_index) ...);
ALTER TABLE rag_chunk ADD FULLTEXT INDEX ft_rag_chunk (title, content) WITH PARSER ngram;
```

CI 的 `migration` job 已升级为：`alembic upgrade head` → 校验 `ALL_TABLES` → **校验 `ft_rag_chunk` 索引存在** → `downgrade base`。

### 本轮修掉的问题

| # | 问题 | 修复 |
| --- | --- | --- |
| 1 | `# 一级标题` 被当成正文块 | 识别 h1 后清空节栈并跳过 |
| 2 | 无标题文档的节路径为空 | 回落到 `default_title` |
| 3 | 带 overlap 的块可能超过 500 字 | 超限时放弃重叠，保证硬上限 |
| 4 | 模型缺 `Text` 导入（ruff 曾判为未使用） | 显式引入并在契约测试中覆盖 |

---

## 迭代 I1 · 第 1 天（2026-10-27）：M1-01 Provider 层

| # | 命令 | 结果 |
| --- | --- | --- |
| 1 | `ruff check .` | ✅ All checks passed（95 文件） |
| 2 | `ruff format --check .` | ✅ 95 files already formatted |
| 3 | `mypy app`（strict） | ✅ no issues in 62 source files |
| 4 | `pytest -q --cov=app` | ✅ **318 passed, 3 skipped，覆盖率 88.41%** |
| 5 | `pytest tests/integration/test_llm_live.py`（真实网关） | ✅ 4 passed（4.2s） |
| 6 | `RUN_LOCAL_EMBED=1 pytest -k fastembed` | ✅ 1 passed（模型下载 48.8s，512 维，语义排序正确） |

### 真实网关能力实测

| 能力 | 结果 | 备注 |
| --- | --- | --- |
| 中文问答 | ✅ | "七天无理由退货"回答准确、引用法条口径 |
| 流式 | ✅ | 首正文 ~2.2s；`reasoning_content` 与 `content` 分离（推理不进用户流） |
| JSON 模式 | ✅ | prompt 必须含 "json"，否则 400（已在客户端前置校验） |
| token 统计 | ✅ | `prompt/completion/total` + `reasoning_tokens` 全部解析 |
| 模型名 | ⚠️ 需 `deepseek-flash` | `DeepSeek-V4.1-Flash` 是展示名，传它返回 400 |
| 首 token 延迟 | ⚠️ 推理模型 ~2.2s | Spec「首 token P95 ≤800ms」只能在**缓存命中**路径达成，M1 不虚报 |

### 迭代中修掉的 5 个真实问题

| # | 问题 | 现象 | 修复 |
| --- | --- | --- | --- |
| 1 | 模型名用展示名 | 400 `The supported API model names are...` | 配置改 `deepseek-flash`，展示名写进文档 |
| 2 | 推理内容混入正文 | 用户会先看到 500~900 字"思考" | provider 分离 `reasoning_content`；SSE 只发 `content` |
| 3 | `onnxruntime 1.31` 导入崩溃 | 进程 access violation（0xC0000005） | 钉 `onnxruntime>=1.17,<1.21`，1.20.1 实测正常 |
| 4 | `deploy/.env` 覆盖根 `.env` | 本地明明配了 `deepseek-flash`，实际跑成 `gpt-4o-mini` | 配置只读根 `.env`；deploy 的 env 仅用于 compose 插值 |
| 5 | PEM 密钥被截断 | RS256 单测 `Could not parse the provided public key` | 密钥解析先识别 `-----BEGIN`，多行内容整体返回 |

### 新增/修改文件

```text
app/llm/{__init__,base,types,openai,mock,embeddings,factory}.py   新增（provider 层）
app/common/config.py       +LLM/RAG/SSE 22 个配置项；env_file 只读根 .env；密钥解析容错
app/api/errors.py          +AS-5033（上游模型不可用）
app/common/logging.py      +get_logger；httpx/httpcore 降到 WARNING
pyproject.toml             +local-embed extra（fastembed + onnxruntime<1.21）；+2 个 marker
deploy/.env.example        LLM/Embedding/RAG/SSE 全量补齐
tests/unit/test_llm_provider.py     MockTransport 精确断言（13 例）
tests/unit/test_embeddings.py       hash/API/fastembed（local_embed 标记）
tests/integration/test_llm_live.py  真实网关 4 例 + 本地模型 1 例
docs/m1/{01-plan,02-tickets,03-verification}.md
```

### 下一步（I1 剩余）

1. **M1-02** 示例语料（3 政策 + 40 FAQ + 1 手册）+ chunker 实现 + `rag_chunk` 迁移（含 ngram FULLTEXT）；
2. **M1-03** 索引管道：批量向量化 → Chroma REST + MySQL → `index_version` 流转 + 一键重建 CLI；
3. **M1-04** 混合召回 + RRF 接线 + Reranker + 双阈值，输出可复现的 Top5 与分数；
4. **M1-00** LangGraph spike 与可观测性接线（可与 1-3 并行）。

> 验收口径：I1 结束时，`python -m app.rag.cli query "退款多久到账"` 能在离线（hash/mock）与真实（fastembed/DeepSeek）两种模式下都返回带分数与出处的 Top5。
