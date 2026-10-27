# M1 Tickets 执行表 · 问答链路

> 方案见 [01-plan.md](01-plan.md)。状态：`TODO` / `DOING` / `DONE`。
> 门禁：每个迭代结束必须 `ruff + mypy + pytest --cov` 本地全绿、GitHub CI 全绿。

## I1 · 检索闭环（第 1 周）

| ID | 任务 | 依赖 | 状态 | 交付/验收 |
| --- | --- | --- | --- | --- |
| M1-00 | LangGraph 兼容 spike（StateGraph / AsyncRedisSaver TTL） | — | TODO | 2 节点 demo + checkpoint 恢复；ADR-0002 |
| M1-01 | LLM/Embedding 抽象 + 真实网关 + mock 回放 | — | **DONE** | `app/llm/*`；真实 DeepSeek 联调 4 例通过；推理内容与正文分离；重试/降级/错误码单测 |
| M1-02 | 示例语料 + chunker 实现 + `rag_chunk` 迁移 | M1-01 | TODO | 500/80 切分、元数据齐全、`alembic upgrade/downgrade` |
| M1-03 | 索引管道（批量向量化 → Chroma + MySQL → index_version） | M1-02 | TODO | 一键重建、doc_count 正确、可回滚 |
| M1-04 | 混合召回 + RRF + Reranker + 双阈值 | M1-03 | TODO | 手算 RRF 对齐；Top1<0.55 不进生成；检索 P95 ≤1.2s |

## I2 · 对话闭环（第 2 周）

| ID | 任务 | 依赖 | 状态 | 交付/验收 |
| --- | --- | --- | --- | --- |
| M1-05 | Prompt Registry + 注入防御 + 出口脱敏 | M1-01 | TODO | 红队 10 条拒绝跨段指令；手机号/身份证打码 |
| M1-06 | LangGraph 图装配（7 节点 + 条件边 + 调用上限） | M1-04 | TODO | 四类意图分流；超时降级；checkpoint 重放 |
| M1-07 | SSE 流式 + citation 事件 + `id:` 续传 | M1-06 | TODO | 首 token 达标（缓存命中）；断线帧连续 |
| M1-08 | 会话记忆（10 轮 + 摘要）+ 指代改写 | M1-06 | TODO | 第 11 轮触发摘要；多轮用例可测 |
| M1-09 | 两级缓存 + 单飞 + 索引版本联动失效 | M1-04 | TODO | 并发同 query 只回源 1 次；切版本无旧结果 |

## I3 · 质量与可观测（第 3 周）

| ID | 任务 | 依赖 | 状态 | 交付/验收 |
| --- | --- | --- | --- | --- |
| M1-10 | 90 条评测用例 + 执行器 + 失败归因 | M1-07 | TODO | `/eval/run` 输出四指标；门禁可阻断 |
| M1-11 | `trace_span` + `/traces/{id}` 调用树 | M1-06 | TODO | 单 trace_id 还原各段耗时/token |
| M1-12 | 指标接线 + Grafana v1 | M1-07 | TODO | 四条核心曲线 |
| M1-13 | 限流 + 会话并发 1 + 输入防护收口 | M1-06 | TODO | 触发 429 AS-4290 |
| M1-14 | CI 扩展（RAG 集成 job + 评测抽样）+ 文档 | M1-10 | TODO | 新 job 全绿，M0 四 job 不退化 |

## M1-01 完成记录（2026-10-27）

| 项 | 结果 |
| --- | --- |
| 真实网关 | `https://api.deepseek.com` + `deepseek-flash`（展示名 DeepSeek-V4.1-Flash）；key 文件 `LLM_KEY=sk-...` 可解析 |
| 实测 | 中文问答正确；流式首正文 ~2.2s（推理模型特性）；JSON 模式可用（prompt 需含 "json"） |
| 关键实现 | `ChatModel`/`EmbeddingModel` 协议；`OpenAICompatibleChatModel`（推理内容独立通道、重试、主备模型降级、错误只带 request_id）；`MockChatModel`（sha256 请求键，miss 即失败） |
| Embedding | `hash`（CI/单测）、`fastembed`（bge-small-zh-v1.5，512 维，实测下载 48.8s，语义排序正确）、`api` |
| 踩坑修复 | ① 模型名必须用 `deepseek-flash`；② `reasoning_content` 不能进用户流；③ `onnxruntime 1.31` 本机导入崩溃 → 钉 `>=1.17,<1.21`；④ `deploy/.env` 会覆盖根 `.env` → 配置只读根 `.env`；⑤ PEM 密钥被"首行优先"截断 → 识别 `-----BEGIN` |
| 门禁 | ruff / ruff format / mypy(strict) 全绿；单测 276 passed；真实联调 4 passed |

### 已知约束（写入 M1 验收口径）

- 推理模型首正文 ~2.2s，Spec「首 token P95 ≤ 800ms」只能在**缓存命中**场景达成；
  非缓存场景按「端到端 P95 ≤ 1.5s + 缓存命中率」组合指标验收，M1 不虚报。
- CI 默认 `LLM_PROVIDER=mock`、`EMBEDDING_PROVIDER=hash`；真实模型只在 staging/本地联调。
