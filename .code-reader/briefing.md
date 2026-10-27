# e-co · 仓库导览

> 离线导览：未调用模型，以下内容来自确定性扫描。配置模型后可 `/refresh` 生成更深入的版本。
> 生成时间：2026-10-27T13:37:31+00:00

## 一句话

对话式售后 Agent：把 「问进度 → 答疑 → 发起退换货 → 审批 → 落库」 收敛成一条对话流水线。 知识问题走 RAG 引用作答，实时数据走只读工具，写操作走状态机 + HITL 双确认 + outbox/MQ 异步落库。

## 技术栈

FastAPI、Starlette/ASGI、Uvicorn、LangGraph、SQLAlchemy、Alembic、MySQL/aiomysql、MySQL/PyMySQL、Redis、RocketMQ、Chroma、pytest；Python（84 个文件）、Markdown（9 个文件）、YAML（7 个文件）、JSON（4 个文件）、PowerShell（4 个文件）、Makefile（1 个文件）、Config（1 个文件）、Dockerfile（1 个文件）

## 入口与运行

- `app/main.py`

运行/测试命令（从 README、Makefile、package.json 等提取）：

- `uvicorn app.main:app --reload --port`
- `uvicorn app.main:app`
- `python -m app.workers.runner`
- `make lint`
- `make format`
- `make type`
- `make cov`
- `make ci`
- `make compose-config`
- `make smoke`

## 目录地图

```text
app/ (64 files)
  api/ (18)
  common/ (7)
  llm/ (7)
  services/ (6)
tests/ (20 files)
  unit/ (16)
  integration/ (2)
  e2e/ (1)
deploy/ (10 files)
  prometheus/ (2)
  alertmanager/ (1)
  grafana/ (1)
  loki/ (1)
docs/ (6 files)
  m0/ (3)
  adr/ (1)
  m1/ (1)
scripts/ (5 files)
evals/ (4 files)
  cases/ (4)
.github/ (1 files)
  workflows/ (1)
loadtest/ (1 files)
root files: .dockerignore, .editorconfig, .gitattributes, .gitignore, .python-version, Makefile, README.md, alembic.ini, pyproject.toml, 电商售后管家-系统开发Spec.md
```

## 关键文档

- `README.md`
- `loadtest/README.md`
- `docs/m0/01-requirements-analysis.md`
- `docs/m0/03-verification.md`
- `电商售后管家-系统开发Spec.md`
- `docs/adr/0001-m0-architecture-decisions.md`
- `docs/runbook.md`
- `docs/m0/02-tickets.md`
- `docs/m1/01-plan.md`

## 建议阅读顺序

1. 先读 README，确认“这个仓库解决什么问题、当前处于哪个阶段”。
2. 再读依赖清单（`pyproject.toml` / `package.json` 等），确认技术栈与运行方式。
3. 从入口文件往下读，画出“请求/程序启动”的第一条链路。
4. 读测试，把测试当成可执行规格，反推设计意图。
5. 最后读 Spec/ADR/验证记录，对照“目标设计”和“已落地实现”。

## 3 个必读文件

1. `README.md` —— 先建立“这个仓库是什么”的整体印象
2. `app/main.py` —— 从真实入口看请求/程序如何启动
3. `loadtest/README.md` —— 补充设计背景、约束或验收标准

## 已实现 vs 规划中

扫描判断：**这个仓库很可能处于早期里程碑或骨架阶段**（文档出现里程碑/M0 等字样；文档出现“骨架/scaffold”；文档提到未实现/501；存在 Spec 类文档）。

因此不能把 Spec 里的目标架构当成已落地实现。读代码时请逐项核对：
- 端点是否返回占位错误或 501；
- 服务方法是否只有契约、没有真实调用；
- 配置/部署是否已经接线；
- `docs/**/verification` 或同类验证记录是否标明未覆盖项。

本导览只能做静态扫描，具体“哪一项已实现”需要在问答中逐文件确认。

## 设计看点

- 请求链路：中间件顺序、trace 传播、SSE 长连接如何共存。
- Agent 编排：状态图节点如何划分，checkpointer 持久化边界在哪。
- 数据层：事务边界、迁移与 outbox 如何保证最终一致。
- Redis：缓存、会话、分布式锁、幂等键是否共用一套 key 规范。
- 消息队列：投递语义、消费幂等、失败重试与死信。

## 风险与限制

- 无
- 本导览是静态扫描，不包含运行时行为。
- 文件路径和行号会在代码变化后失效；回答具体问题时应重新读取。
- 仓库状态：git 仓库，分支 `main`，HEAD `523e33a`。


---

> 本导览由 code-reader 离线生成；配置模型后 `/refresh` 可获得更深入的版本。
