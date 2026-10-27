# M1 验证记录 · 问答链路

> 逐迭代留痕：做了什么、跑了什么、数字是多少、哪些是"假绿"必须排除。

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
