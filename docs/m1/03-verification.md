# M1 验证记录 · 问答链路

> 逐迭代留痕：做了什么、跑了什么、数字是多少、哪些是"假绿"必须排除。

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
