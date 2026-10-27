# 电商售后管家 · 系统开发 Spec

| 项目 | 内容 |
| --- | --- |
| 文档版本 | **v2.0（生产就绪版）** |
| 状态 | 生产上线评审 |
| 创建日期 | 2026-10-27 |
| 最近修订 | 2026-10-27 |
| 文档类型 | 系统设计与开发规格说明书（可上线标准） |
| 技术栈 | Python 3.11 / FastAPI / LangChain / LangGraph / Chroma / MySQL / Redis / RocketMQ(Kafka) / Docker Compose / Prometheus·Grafana·Alertmanager·Loki |

**修订记录**

| 版本 | 日期 | 修订说明 |
| --- | --- | --- |
| v1.0 | 2026-10-27 | 初版：分层架构、功能需求、状态机、接口、数据与消息设计 |
| v2.0 | 2026-10-27 | **生产就绪改造**：SLO 与错误预算、高可用拓扑、灰度发布与回滚、备份容灾、告警值班、运营后台 API、上线 Go-Live 清单 |

---

## 1. 概述

### 1.1 背景

电商售后链路长、入口分散：买家需要在订单、物流、工单、客服多个入口之间来回切换；退款进度依赖人工追问，高峰期客服排队；退款、退换货等高风险动作容易误触、容易重复提交。

本系统把 **「问进度 → 答疑 → 发起退换货 → 审批 → 落库」** 收敛为一条对话流水线：知识类问题走 RAG 引用作答，实时数据走业务工具，写操作走状态机 + HITL 审批 + MQ 异步落库。

### 1.2 目标（可验收）

| 编号 | 目标 | 验收标准 |
| --- | --- | --- |
| G1 | 一句话完成查询与办理 | 单轮或少轮对话内完成「查询订单/物流」与「发起退换货」 |
| G2 | 知识问答可溯源 | 所有知识类回答携带引用（文档片段 + 出处），无引用不得直出 |
| G3 | 高风险动作零误触 | 写操作 100% 经过二次确认；重复提交不产生重复单据 |
| G4 | 问答链路稳定 | 缓存命中场景下对话接口 P95 ≤ 1.5s（不含首 token 流式延迟） |
| G5 | 办事链路最终一致 | MQ 消费失败可恢复，单据状态不出现非法跃迁 |
| G6 | 全链路可回放 | 任一 `trace_id` 可还原 API → Agent → Tool → RAG → MQ → DB 全过程 |
| G7 | **生产级可用性** | 核心接口月度可用性 ≥ **99.9%**（错误预算 ≤ 43min/月）；任一单点故障不导致全域不可用 |
| G8 | **不停机演进** | Prompt / 索引 / 模型 / 灰度比例可在线发布，回滚 ≤ 15min |

### 1.3 范围

**范围内（上线必备）**

- 对话式问答：订单/物流/库存/退款进度查询、售后政策类知识问答（RAG）
- 对话式办理：发起退货/换货/退款申请，缺参澄清，进度跟踪
- HITL：高风险写操作二次确认、人工审批节点与回调
- **真实业务接口对接**：订单/物流/退款 Provider 适配层 + 契约测试（mock 仅限 dev 环境；staging 联调完成是上线硬条件，见 §21 Q1）
- **生产运维能力**：高可用部署、灰度发布与回滚、监控告警与值班、备份恢复演练、管理后台 API
- 工程能力：缓存与会话、幂等与状态机、MQ 异步落库、Trace/Eval/压测

**范围外（后续迭代）**

- 多租户与开放平台对接、App 原生 SDK
- 可视化运营前端（本期提供管理 API，Web 工作台/运营前端进二期）
- 推荐/营销类对话能力
- 跨境售后、逆向物流轨迹承运商对接

### 1.4 用户角色

| 角色 | 说明 | 关键权限 |
| --- | --- | --- |
| buyer（买家） | C 端对话用户 | 查询本人订单；发起售后；确认写操作 |
| agent（客服） | B 端工作台操作员 | 查询任意订单；代客发起售后；查看 trace |
| approver（审批人） | 售后审核人员 | 审批 HITL 任务（通过/驳回） |
| ops（运营） | 知识库与评测维护者 | 维护文档/索引版本；运行评测集 |
| admin（管理员） | 系统管理 | Prompt 版本、熔断开关、权限管理 |

### 1.5 术语

| 术语 | 定义 |
| --- | --- |
| 售后单（biz） | 一次退换货/退款申请的业务单据，唯一标识 `bizId` |
| 幂等键 | `bizId + action` 复合键，用于防重复提交与消费去重 |
| HITL | Human-in-the-loop，高风险动作的人工确认/审批节点 |
| outbox | 本地消息表，保证业务落库与消息发送的最终一致 |
| 索引版本 | RAG 向量索引的版本号，用于缓存联动失效与回滚 |
| 低置信兜底 | 检索分数低于阈值时的降级策略（澄清/转人工） |
| 双链路 | 问答链路（同步、低延迟）与办事链路（异步、最终一致） |

---

## 2. 总体架构

### 2.1 分层架构

```mermaid
flowchart TB
    subgraph L1["接入层"]
        A1["App / 小程序"]
        A2["Web 客服工作台"]
        A3["API 网关 · JWT 鉴权 · 限流 · SSE"]
    end
    subgraph L2["应用服务层 · FastAPI"]
        B1["对话会话服务 /chat/stream"]
        B2["售后业务接口 /aftersales/*"]
        B3["评测 /eval/run"]
        B4["Trace 中间件 · trace_id"]
    end
    subgraph L3["Agent 编排层 · LangGraph"]
        C1["意图路由"]
        C2["澄清追问"]
        C3["RAG 引用作答"]
        C4["售后状态机"]
        C5["HITL 审批"]
    end
    subgraph L4["能力层"]
        D1["RAG 管道 · 混合召回 · Reranker"]
        D2["业务工具 · 只读/写分级"]
        D3["Prompt Registry · 注入防御"]
        D4["低置信兜底"]
    end
    subgraph L5["数据与中间件"]
        E1["MySQL · 业务库 + outbox"]
        E2["Redis · 缓存/会话/锁/幂等"]
        E3["RocketMQ / Kafka"]
        E4["Chroma · 向量索引"]
    end
    L1 --> L2 --> L3
    L3 --> L4
    L4 --> L5
```

**双链路原则（架构第一决策）**

| 链路 | 路径 | 一致性模型 | 延迟目标 |
| --- | --- | --- | --- |
| 问答链路 | 意图路由 → RAG 混合召回 → 引用作答 | 强一致读（缓存可接受秒级） | P95 ≤ 1.5s |
| 办事链路 | 工具提参 → 状态机 → HITL → 同步受理 + MQ 异步落库 | 最终一致（outbox + 幂等） | 受理同步返回 ≤ 500ms |

### 2.2 技术选型与职责

| 组件 | 选型 | 职责 |
| --- | --- | --- |
| 应用框架 | FastAPI (Python 3.11) | REST + SSE、依赖注入、参数校验、统一异常 |
| Agent 编排 | LangGraph + LangChain | 状态图、条件路由、工具调用、checkpointer 持久化 |
| 向量库 | Chroma | 文档分块向量化、混合召回的向量通道 |
| 主库 | MySQL 8.0 | 业务单据、审批、outbox、评测、Prompt 注册表 |
| 缓存/会话 | Redis 7 | 热点缓存、Session Memory、分布式锁、幂等、限流 |
| 消息队列 | RocketMQ（可切换 Kafka） | 工单落库、审批回调、评测与日志异步化、削峰 |
| 部署 | Docker Compose（dev/staging）+ 双副本生产拓扑 | 单机编排、健康检查、滚动升级 |
| 可观测栈 | Prometheus + Grafana + Alertmanager + Loki | 指标采集、SLO 看板、告警、日志检索 |
| 压测/评测 | JMeter + /eval/run | 容量评估与回归门禁 |

### 2.3 生产部署拓扑

```mermaid
flowchart LR
    U["客户端"] --> WAF["WAF/CDN · TLS 终止"]
    WAF --> LB["LB · 健康检查/摘流"]
    subgraph P["生产应用层（跨节点·无状态）"]
        A1["api × 2"]
        A2["worker × 2"]
    end
    LB --> A1
    A1 --> DB["MySQL 主从 · 半同步/云RDS"]
    A1 --> RD["Redis 哨兵 · 自动切换"]
    A1 --> MQ["RocketMQ 主从"]
    A1 --> CH["Chroma · 快照+可重建"]
    A2 --> DB
    A2 --> RD
    A2 --> MQ
    A2 --> CH
    A1 --> OBS["Prometheus · Grafana · Alertmanager · Loki"]
    A2 --> OBS
```

- **依赖组件优先云托管**（RDS 半同步 / 云 Redis 哨兵 / 云 MQ 主从），自建为兜底；应用层无状态，可水平扩容与滚动升级。
- `api` 与 `worker` 使用**同一镜像、不同入口命令**（`uvicorn app.main:app` / `python -m app.workers.runner`）。
- 详见 §17 生产部署与配置。

---

## 3. 功能需求

> 编号规则：`FR-<模块>-<序号>`；每条含「需求描述 + 验收标准」。

### 3.1 对话问答（FR-CHAT）

| 编号 | 需求描述 | 验收标准 |
| --- | --- | --- |
| FR-CHAT-01 | 意图识别与路由：区分「知识问答 / 数据查询 / 办理售后 / 闲聊拒绝」 | 四类意图识别准确率 ≥ 90%（评测集） |
| FR-CHAT-02 | 多轮指代改写：结合会话历史改写 Query（"那这一单呢"） | 改写后检索命中率较不改写提升 ≥ 15% |
| FR-CHAT-03 | 缺参澄清：工具必需参数缺失时生成澄清问题，携带上下文继续 | 澄清后一轮内补齐参数成功率 ≥ 85% |
| FR-CHAT-04 | RAG 引用作答：回答必须携带 chunk 级引用与出处 | 无引用的知识回答一律拦截（低置信兜底） |
| FR-CHAT-05 | 低置信兜底：Top1 分数 < 阈值时澄清或转人工 | 兜底触发时不得编造答案 |
| FR-CHAT-06 | SSE 流式输出：打字机式返回 token 与工具执行事件 | 首 token 延迟 P95 ≤ 800ms（热缓存） |
| FR-CHAT-07 | 会话记忆：滑动窗口 + 超长自动摘要压缩 | 超过 10 轮自动摘要，上下文不超模型窗口 |
| FR-CHAT-08 | 闲聊与越界拒绝：非售后问题礼貌拒绝并引导 | 拒绝话术命中评测集 100% |

### 3.2 售后办理（FR-AS）

| 编号 | 需求描述 | 验收标准 |
| --- | --- | --- |
| FR-AS-01 | 只读查询工具：订单、物流、库存、退款进度 | 单工具超时 ≤ 800ms，超时降级提示 |
| FR-AS-02 | 发起退货/换货/退款：工具提参 → 状态机受理 | 合法请求 100% 生成售后单并返回 bizId |
| FR-AS-03 | 幂等防重：`bizId+action` 幂等键 + 分布式锁 | 并发 10 次重复提交仅产生 1 张单据 |
| FR-AS-04 | 进度查询：`查一下我那笔退款` → 单据状态实时读取 | 读取结果与 DB 单据状态一致（读己之写） |
| FR-AS-05 | 状态机合法迁移校验：非法跃迁直接拒绝 | 覆盖迁移表全部非法用例（评测集） |
| FR-AS-06 | 中断恢复：LangGraph checkpoint 支持审批挂起后续跑 | 进程重启后待审批任务可恢复上下文 |

### 3.3 审批与 HITL（FR-HITL）

| 编号 | 需求描述 | 验收标准 |
| --- | --- | --- |
| FR-HITL-01 | 写操作双确认：金额/退款类动作需用户明确确认 | 未确认前不调用写工具 |
| FR-HITL-02 | 人工审批节点：超过阈值的退款进入 approver 队列 | 阈值可配置，默认 500 元 |
| FR-HITL-03 | 审批回调：`POST /approval/callback` 驱动状态推进 | 回调幂等，重复回调结果一致 |
| FR-HITL-04 | 高风险熔断：写链路错误率 > 阈值时熔断降级转人工 | 熔断开关实时生效，可热恢复 |

### 3.4 缓存与会话（FR-CACHE）

| 编号 | 需求描述 | 验收标准 |
| --- | --- | --- |
| FR-CACHE-01 | 三级缓存：商品事实（长 TTL）/ FAQ（中）/ 检索结果（短） | TTL 可配置，命中率统计可用 |
| FR-CACHE-02 | 双检锁防击穿：热点 key 过期瞬间只打一次源 | 并发穿透压测无雪崩 |
| FR-CACHE-03 | 索引版本联动失效：索引重建时整组检索缓存失效 | 版本切换后无旧索引结果返回 |
| FR-CACHE-04 | 热 key 治理：本地 Caffeine 进程内缓存 + 分布式缓存 | 重复 Embedding/LLM 调用下降 ≥ 60% |

### 3.5 评测与可观测（FR-OBS）

| 编号 | 需求描述 | 验收标准 |
| --- | --- | --- |
| FR-OBS-01 | trace_id 贯穿：API / Agent / Tool / RAG / MQ / DB | 单 ID 可查询完整调用树 |
| FR-OBS-02 | `/eval/run` 回归：典型/多跳/无答案/多轮四类用例 | 输出完成率、引用正确率、延迟三指标 |
| FR-OBS-03 | 失败归因：评测失败标注失败节点（路由/检索/工具） | 归因准确率抽检 ≥ 90% |
| FR-OBS-04 | 成本治理：按 trace 统计 token 与 LLM 调用成本 | 支持按天/按功能聚合报表 |
| FR-OBS-05 | 压测门禁：JMeter 脚本随版本维护 | 问答 P95 不劣化超过 10% 方可发布 |

### 3.6 上线运营（FR-OPS）

| 编号 | 需求描述 | 验收标准 |
| --- | --- | --- |
| FR-OPS-01 | 灰度放量：按 `hash(user_id) % 100` 百分比分流，运行时可调 | 10%→50%→100% 三档 staging 实测通过 |
| FR-OPS-02 | 在线回滚：镜像 / Prompt / 索引版本一键回退 | 回滚全程 ≤ 15min（含观察确认） |
| FR-OPS-03 | 管理后台 API：熔断开关、阈值、灰度、发布、看板 | 全部变更操作审计留痕（who/what/before/after） |
| FR-OPS-04 | 人工接管：兜底/熔断/超时会话自动转入人工队列 | 转人工请求 30s 内进入客服队列 |
| FR-OPS-05 | 告警值班：SLO 违约、积压、死信、熔断自动告警 | P1 告警 5min 内触达值班人 |
| FR-OPS-06 | 备份恢复演练：脚本化恢复，按 RTO/RPO 验收 | 每季度演练通过并归档记录 |

---

## 4. 业务流程

### 4.1 问答链路时序

```mermaid
sequenceDiagram
    participant U as 买家
    participant API as FastAPI /chat/stream
    participant AG as LangGraph
    participant RG as RAG管道
    participant CH as Redis/Chroma

    U->>API: POST /chat/stream (SSE)
    API->>API: 生成 trace_id、鉴权、限流
    API->>AG: 运行状态图(checkpointer=会话)
    AG->>AG: 意图路由(知识问答)
    AG->>AG: Query改写(多轮指代)
    AG->>CH: 查检索缓存(索引版本绑定)
    alt 缓存未命中
        AG->>RG: 混合召回 + Reranker
        RG-->>AG: chunks + scores
        AG->>CH: 回写检索缓存(TTL短)
    end
    alt Top1 分数 ≥ 阈值
        AG-->>U: SSE: 引用作答(含出处)
    else 低置信
        AG-->>U: SSE: 澄清追问 / 转人工兜底
    end
    API->>CH: 写会话记忆(滑动窗口)
```

### 4.2 办事链路时序（退换货 + HITL）

```mermaid
sequenceDiagram
    participant U as 买家
    participant API as FastAPI
    participant AG as LangGraph状态机
    participant TL as 业务工具
    participant LK as Redis锁/幂等
    participant DB as MySQL
    participant MQ as RocketMQ
    participant WK as worker消费者
    participant AP as 审批人

    U->>API: "我要退XX订单，退款"
    API->>AG: 运行状态图
    AG->>TL: 只读工具取订单/库存(缺参则澄清)
    AG->>AG: 进入状态机 PENDING_APPROVAL
    AG->>LK: SETNX bizId+action (幂等)
    AG->>DB: INSERT 售后单 + outbox(同事务)
    AG->>MQ: 发送 outbox 消息(异步投递)
    AG-->>U: 同步受理(返回 bizId + 待审批)
    MQ->>WK: 工单消息
    WK->>DB: 消费落库(幂等键去重)
    AP-->>API: POST /approval/callback (通过)
    API->>DB: 状态迁移 APPROVED(乐观锁)
    DB-->>U: 通知(站内/SSE事件)
    Note over DB,WK: 失败重试→死信告警→人工重投
```

---

## 5. 售后状态机设计

### 5.1 状态定义

```mermaid
stateDiagram-v2
    [*] --> DRAFT: 提交申请
    DRAFT --> PENDING_APPROVAL: 参数齐全提交
    DRAFT --> [*]: 用户放弃
    PENDING_APPROVAL --> APPROVED: 审批通过
    PENDING_APPROVAL --> REJECTED: 审批驳回
    PENDING_APPROVAL --> CANCELED: 用户取消/超时
    APPROVED --> PROCESSING: 工单执行
    APPROVED --> CANCELED: 取消执行
    REJECTED --> [*]
    PROCESSING --> DONE: 完成(退款到账/换货签收)
    PROCESSING --> FAILED: 执行失败(可重试)
    FAILED --> PROCESSING: 重试
    FAILED --> [*]: 重试耗尽转人工
    DONE --> [*]
    CANCELED --> [*]
```

### 5.2 状态迁移表（唯一权威来源）

| 当前状态 | 事件 | 目标状态 | 触发方 | 守卫条件 |
| --- | --- | --- | --- | --- |
| DRAFT | SUBMIT | PENDING_APPROVAL | Agent/用户确认 | 参数校验通过 + 幂等键未占用 |
| DRAFT | ABANDON | （终态删除） | 用户 | — |
| PENDING_APPROVAL | APPROVE | APPROVED | approver 回调 | 状态版本号匹配 |
| PENDING_APPROVAL | REJECT | REJECTED | approver 回调 | 状态版本号匹配 |
| PENDING_APPROVAL | CANCEL | CANCELED | 用户/超时(24h) | 无执行中任务 |
| APPROVED | EXECUTE | PROCESSING | worker | 工单创建成功 |
| APPROVED | CANCEL | CANCELED | 用户/客服 | — |
| PROCESSING | COMPLETE | DONE | worker/回调 | 逆向物流签收/退款到账 |
| PROCESSING | FAIL | FAILED | worker | 错误码可重试 |
| FAILED | RETRY | PROCESSING | worker | 重试次数 < 3 |
| FAILED | ESCALATE | （人工工单） | 熔断/耗尽 | — |

**约束（必须由代码强校验）**

1. 迁移函数是**唯一**修改状态的入口，禁止散落的 `UPDATE ... SET status=`。
2. 每次迁移携带 `version` 乐观锁：`UPDATE ... SET status=?, version=version+1 WHERE biz_id=? AND version=?`，影响行数为 0 即冲突，按最新状态决策（重读重放或返回 409）。
3. 非表内迁移一律抛 `IllegalTransitionError`（错误码 `AS-4003`），写入失败日志并纳入评测集。

### 5.3 状态数据结构（LangGraph state 片段）

```python
class AfterSalesState(TypedDict):
    trace_id: str
    session_id: str
    user_id: str
    intent: Literal["qa", "query", "after_sales", "chitchat"]
    rewritten_query: str | None
    slots: dict  # 订单号、SKU、原因、金额等待补参数槽位
    missing_slots: list[str]
    tool_results: list[ToolResult]
    citations: list[Citation]  # chunk级引用
    biz_id: str | None
    action: Literal["refund", "return", "exchange"] | None
    status: AfterSalesStatus  # 见 5.1
    version: int  # 乐观锁版本
    confirm_required: bool  # 是否已过双确认
    approval_task_id: str | None
    answer_stream: str  # SSE 增量输出
    error: AppError | None
```

---

## 6. Agent 编排设计（LangGraph）

### 6.1 图结构

```mermaid
flowchart TD
    START([start]) --> ROUTER{意图路由}
    ROUTER -->|qa| REWRITE[Query改写]
    ROUTER -->|query| TOOLS_RO[只读工具]
    ROUTER -->|after_sales| TOOLS_RO
    ROUTER -->|chitchat| REJECT[越界拒绝]
    REWRITE --> RETRIEVE[RAG混合召回+重排]
    RETRIEVE -->|高置信| ANSWER[引用作答]
    RETRIEVE -->|低置信| CLARIFY[澄清/兜底]
    TOOLS_RO -->|缺参| CLARIFY2[缺参澄清]
    CLARIFY2 --> TOOLS_RO
    TOOLS_RO -->|参数齐| CONFIRM{写操作?}
    CONFIRM -->|只读| SSE_OUT[流式输出]
    CONFIRM -->|写操作| HITL1{用户双确认}
    HITL1 -->|确认| SM[售后状态机受理]
    HITL1 -->|拒绝| SSE_OUT
    SM -->|需人工审批| APPROVAL[HITL审批挂起]
    SM -->|无需审批| SSE_OUT
    APPROVAL -->|callback恢复| SSE_OUT
    ANSWER --> SSE_OUT
    CLARIFY --> SSE_OUT
    REJECT --> SSE_OUT
    SSE_OUT --> END([end])
```

### 6.2 节点与边规范

| 节点 | 类型 | 职责 | 超时 | 失败策略 |
| --- | --- | --- | --- | --- |
| `router` | 条件边 | 结构化输出分类四意图 | 2s | 规则兜底（关键词）→ 仍失败转 qa |
| `rewrite` | 智能节点 | 多轮指代改写 | 3s | 失败用原始 query 继续 |
| `retrieve` | 混合节点 | 缓存→混合召回→Reranker | 1.2s | 超时返回缓存旧值或兜底 |
| `answer` | 智能节点 | 生成带引用回答 | 8s | 重试 1 次 → 兜底话术 |
| `tools_ro` | 工具节点 | 只读工具调用（并行） | 单工具 0.8s | 超时降级提示，不阻塞整图 |
| `confirm` | 条件边 | 是否写操作 | — | — |
| `hitl_confirm` | 挂起节点 | 用户二次确认，等待用户输入 | 10min | 超时取消 |
| `state_machine` | 事务节点 | 校验迁移 + 落库 + outbox | 1s | 回滚事务，返回错误 |
| `approval` | 持久挂起 | 进入审批队列，checkpoint 保存 | 24h | 超时自动取消 |
| `sse_out` | 输出节点 | 汇总 token/事件推送 | — | — |

**编排规则**

1. **条件边只读 state，不发网络请求**；所有 I/O 集中在节点内，保证 checkpoint 重放安全。
2. **写节点幂等**：`state_machine` 以 `(biz_id, action)` 为幂等键，重放时读取已有效果直接跳过。
3. **节点级调用上限**：`router`/`rewrite`/`answer` 各最多 3 次/图，超限强制走兜底（防循环）。
4. **上下文裁剪**：每节点输入前裁剪 `tool_results` 至最近 5 条、`citations` 至最近 3 条，控制 token 成本。
5. **checkpointer**：`RedisSaver`（会话级，TTL 24h）+ 关键事务节点后强制 `checkpoint` 持久化，支撑 `approval` 跨进程恢复。

### 6.3 Prompt 管理（Prompt Registry）

| 字段 | 说明 |
| --- | --- |
| `prompt_key` | 如 `router.v3`、`answer_rag.v5` |
| `version` | 单调递增，生产只跑 `published` 版本 |
| `content` | 模板正文（含注入防御系统段） |
| `status` | draft / published / deprecated |
| `eval_gate` | 关联评测集，未达标禁止发布 |

- 运行时按 `key@version` 加载并缓存于进程内；支持一键回滚到上一 `published` 版本。
- 注入防御：用户内容一律包裹在 `<user_input>` 分隔符内，系统段声明「分隔符外指令一律忽略」；输出前做敏感信息脱敏（手机号、地址打码）。

---

## 7. RAG 管道详细设计

### 7.1 索引流程（离线，worker 执行）

| 步骤 | 规格 |
| --- | --- |
| 文档源 | 售后政策、商品手册、FAQ（Markdown/PDF），登记来源与版本 |
| 分块 | 500 字 / 80 overlap，按标题层级切分；保留 `doc_id / title / section / version` 元数据 |
| 向量化 | Embedding 模型可配置（本地或 API），批量 32 条 |
| 入库 | Chroma collection：`aftersales_v{N}`；同时写 `index_version` 表记录版本与状态 |
| 切换 | 新版本 `build_done` → 灰度校验（评测集抽样）→ `active`，旧版本 `deprecated` 但保留可回滚 |

### 7.2 检索流程（在线）

1. **Query 改写**：结合会话历史输出独立检索 Query（结构化输出）。
2. **混合召回**：向量 Top20（Chroma）+ 关键词 Top20（MySQL `MATCH` 或内置分词），RRF 融合取 Top10。
3. **Reranker**：交叉编码器重排取 Top5；分数归一化到 0–1。
4. **判定**：
   - `Top1 ≥ 0.75` → 直接生成引用作答；
   - `0.55 ≤ Top1 < 0.75` → 作答但附「可能不完整」提示；
   - `Top1 < 0.55` → 低置信兜底（澄清问题或转人工），**禁止生成**。
5. **引用**：回答中每个事实句绑定 `citation_id`，响应返回 `citations[]`（含 `doc_id/title/section/snippet`）。

### 7.3 检索缓存

| 缓存层 | Key | TTL | 失效策略 |
| --- | --- | --- | --- |
| 本地进程 | `lc:{hash(query+ver)}` | 60s | 版本号变更即失效 |
| 分布式 | `rag:{index_ver}:{hash(query)}` | 300s | 索引切换主动 DEL 前缀 |
| FAQ 命中 | `faq:{hash(question)}` | 30min | 文档版本更新失效 |
| 商品事实 | `fact:sku:{sku_id}` | 10min | 双检锁（SETNX + 5s 过期 + 本地延迟过期） |

---

## 8. 工具层设计

### 8.1 工具清单

| 工具名 | 级别 | 入参 | 返回 | 超时 | 降级 |
| --- | --- | --- | --- | --- | --- |
| `get_order` | 只读 | `order_id` | 订单头+行项目 | 800ms | 缓存 60s 快照 |
| `get_logistics` | 只读 | `order_id` | 轨迹节点列表 | 800ms | 「轨迹延迟」提示 |
| `get_stock` | 只读 | `sku_id, qty` | 可用库存 | 500ms | 0.5s 缓存 |
| `get_refund_progress` | 只读 | `order_id` | 退款阶段/预计时间 | 800ms | 引导查进度页 |
| `check_aftersales_policy` | 只读 | `category, days` | 适用政策(RAG) | 1.2s | 兜底话术 |
| `create_after_sales` | **写** | `order_id, action, reason, sku_id?, amount?` | `biz_id` | 1s | 事务回滚 |
| `cancel_after_sales` | **写** | `biz_id, action="cancel"` | 结果 | 1s | 事务回滚 |

### 8.2 工具调用规范

1. **Schema 强校验**：Pydantic 定义参数模型，LLM 结构化输出不合 schema 直接触发澄清（FR-CHAT-03）。
2. **分级授权**：只读工具任意角色可用；写工具要求 `buyer` 仅限本人订单、`agent` 需审计日志、`approver` 仅审批回调。
3. **写操作守卫**：`create_after_sales` 调用前必须满足 `confirm_required=True` 且用户已在当前轮明确确认（双确认事件入 trace）。
4. **超时与并发**：只读工具 `asyncio.gather` 并行；单工具熔断器（连续 5 次超时 → 熔断 30s）。
5. **审计**：所有写工具调用记录 `trace_id / user_id / tool / params_hash / result` 到 `tool_audit` 表。

---

## 9. API 接口规格

### 9.1 通用约定

| 项 | 约定 |
| --- | --- |
| 鉴权 | `Authorization: Bearer <JWT>`；payload 含 `user_id / role / exp` |
| 追踪 | 请求头 `X-Trace-Id`（缺省则网关生成）；响应头原样返回 |
| 幂等 | 写接口请求头 `Idempotency-Key: {bizId}:{action}`；重复请求返回首次结果 |
| 版本 | 路径前缀 `/api/v1` |
| 时间 | ISO8601 UTC（`2026-10-27T08:00:00Z`） |
| 成功响应 | `{"code": 0, "message": "ok", "data": {...}, "trace_id": "..."}` |
| 失败响应 | `{"code": "AS-xxxx", "message": "...", "detail": {...}, "trace_id": "..."}` |

**业务错误码（节选）**

| code | HTTP | 含义 |
| --- | --- | --- |
| AS-4001 | 400 | 参数校验失败 |
| AS-4002 | 422 | 业务规则不允许（如订单不可售后） |
| AS-4003 | 409 | 状态机非法迁移 / 版本冲突 |
| AS-4004 | 409 | 幂等冲突（同键不同参） |
| AS-4010 | 401 | 鉴权失败 |
| AS-4030 | 403 | 权限不足（写操作分级） |
| AS-4290 | 429 | 限流 |
| AS-5001 | 500 | 内部错误 |
| AS-5031 | 503 | 熔断中（写链路降级转人工） |

### 9.2 接口清单

#### POST `/api/v1/chat/stream` — 对话（SSE）

请求体：

```json
{
  "session_id": "sess_01H...",
  "message": "我的退款到哪了",
  "context": {"channel": "app", "entry": "order_detail"}
}
```

SSE 事件协议（`event: <type>\ndata: <json>`）：

| event | data | 时机 |
| --- | --- | --- |
| `delta` | `{"text": "..."}` | LLM token 增量 |
| `tool` | `{"name": "get_refund_progress", "status": "start\|end", "ms": 120}` | 工具执行 |
| `clarify` | `{"question": "...", "missing": ["order_id"]}` | 缺参/低置信澄清 |
| `confirm` | `{"action": "refund", "amount": 199.0, "biz_hint": "..."}` | 写操作双确认请求 |
| `citation` | `{"id": 1, "doc": "售后政策", "section": "3.2", "snippet": "..."}` | 引用块 |
| `status` | `{"biz_id": "AS2026...", "status": "PENDING_APPROVAL"}` | 受理成功 |
| `error` | `{"code": "AS-5031", "message": "..."}` | 任何失败 |
| `done` | `{"trace_id": "...", "usage": {"tokens": 812}}` | 结束 |

> 客户端确认语义：收到 `confirm` 事件后，用户点「确认」会作为新一轮 message 进入，消息内携带 `confirm_token`，服务端校验后才执行写工具。

#### GET `/api/v1/aftersales/{biz_id}` — 单据进度

响应 `data`：

```json
{
  "biz_id": "AS20261027001",
  "action": "refund",
  "status": "PROCESSING",
  "version": 4,
  "order_id": "O2026...",
  "amount": 199.00,
  "created_at": "2026-10-27T09:12:00Z",
  "timeline": [
    {"status": "PENDING_APPROVAL", "at": "2026-10-27T09:12:00Z"},
    {"status": "APPROVED", "at": "2026-10-27T09:20:11Z"}
  ]
}
```

#### POST `/api/v1/aftersales/apply` — 发起售后（写，幂等）

请求头：`Idempotency-Key: {bizId}:create`

```json
{
  "order_id": "O2026...",
  "action": "refund",
  "reason": "quality_issue",
  "sku_id": "SKU123",
  "amount": 199.00,
  "confirm_token": "ct_9f3a..."
}
```

- `confirm_token`：由 `confirm` 事件签发，单次有效、5 分钟过期；缺失或失效 → `AS-4001`。
- 响应 `data`：`{"biz_id": "AS...", "status": "PENDING_APPROVAL", "approval_required": true}`。

#### POST `/api/v1/approval/callback` — 审批回调（写，幂等）

```json
{"approval_task_id": "ap_...", "decision": "approve", "approver": "u_889", "comment": "符合七天无理由"}
```

- 验签：`X-Signature = HMAC-SHA256(secret, body)`；重复回调返回首次结果（幂等）。

#### POST `/api/v1/eval/run` — 评测回归（ops/admin）

```json
{"suite": "nightly", "categories": ["typical", "multi_hop", "no_answer", "multi_turn"], "baseline": "2026-10-26"}
```

响应：`run_id` + 各维度得分（完成率 / 引用正确率 / P95 延迟 / 归因分布）。

#### GET `/api/v1/traces/{trace_id}` — 链路查询（agent/admin）

返回调用树：`api → node → tool → rag → mq → db` 各段耗时、LLM token、缓存命中标志。

#### 其余

| 方法路径 | 说明 |
| --- | --- |
| `POST /api/v1/auth/login` | 登录换 JWT |
| `GET /api/v1/orders/{order_id}` | 订单只读查询（工作台） |
| `POST /api/v1/aftersales/{biz_id}/cancel` | 取消单据（幂等） |
| `GET /healthz` / `GET /readyz` | 存活/就绪探针（`/readyz` 校验 MySQL/Redis/MQ/Chroma 连通与迁移版本） |

#### 管理与运营接口（`admin`/`ops` 角色，全部写审计）

| 方法路径 | 说明 |
| --- | --- |
| `POST /api/v1/admin/prompts/{key}/publish` | 发布或回滚指定 Prompt 版本（body: `{"version": 6}`） |
| `POST /api/v1/admin/index/{ver}/activate` | 激活/回切索引版本（旧版自动 deprecated，缓存随版本号失效） |
| `POST /api/v1/admin/breaker` | 熔断开关：`{"scope": "write\|llm\|all", "open": true}` |
| `POST /api/v1/admin/gray` | 设置灰度放量百分比 |
| `GET /api/v1/admin/dashboard` | 看板聚合：解决率、转人工率、意图分布、P95、错误率、outbox 积压、LLM 成本 |
| `POST /api/v1/admin/sessions/{id}/takeover` | 会话转入人工队列 |
| `GET /api/v1/admin/audit` | 变更审计查询 |

---

## 10. 数据库设计（MySQL）

> 库：`aftersales`；字符集 `utf8mb4`；所有写表含 `created_at / updated_at / version`。

### 10.1 核心表 DDL

```sql
-- 售后单（办事链路主表）
CREATE TABLE aftersales_biz (
  id            BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
  biz_id        VARCHAR(32)  NOT NULL,                -- 对外唯一单号
  user_id       VARCHAR(64)  NOT NULL,
  order_id      VARCHAR(32)  NOT NULL,
  action        ENUM('refund','return','exchange') NOT NULL,
  status        VARCHAR(32)  NOT NULL,                -- 见状态机
  amount        DECIMAL(10,2) NULL,
  reason_code   VARCHAR(32)  NULL,
  idem_key      VARCHAR(80)  NOT NULL,                -- biz_id:action
  version       INT UNSIGNED NOT NULL DEFAULT 0,      -- 乐观锁
  trace_id      VARCHAR(64)  NOT NULL,
  created_at    DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  updated_at    DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3) ON UPDATE CURRENT_TIMESTAMP(3),
  UNIQUE KEY uk_biz (biz_id),
  UNIQUE KEY uk_idem (idem_key),
  KEY idx_user_order (user_id, order_id),
  KEY idx_status_time (status, created_at)
) ENGINE=InnoDB;

-- 状态流水（审计 + 时间线）
CREATE TABLE aftersales_transition (
  id          BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
  biz_id      VARCHAR(32) NOT NULL,
  from_status VARCHAR(32) NOT NULL,
  to_status   VARCHAR(32) NOT NULL,
  event       VARCHAR(32) NOT NULL,
  actor       VARCHAR(64) NOT NULL,                   -- user:xxx / system / approver:xxx
  version     INT UNSIGNED NOT NULL,
  trace_id    VARCHAR(64) NOT NULL,
  created_at  DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  KEY idx_biz (biz_id, created_at)
) ENGINE=InnoDB;

-- HITL 审批任务
CREATE TABLE approval_task (
  id          BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
  task_id     VARCHAR(32) NOT NULL,
  biz_id      VARCHAR(32) NOT NULL,
  status      ENUM('pending','approved','rejected','expired') NOT NULL DEFAULT 'pending',
  threshold   DECIMAL(10,2) NOT NULL,
  approver    VARCHAR(64) NULL,
  comment     VARCHAR(255) NULL,
  callback_count INT UNSIGNED NOT NULL DEFAULT 0,     -- 回调幂等计数
  expires_at  DATETIME NOT NULL,
  created_at  DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  UNIQUE KEY uk_task (task_id),
  KEY idx_biz (biz_id),
  KEY idx_pending (status, expires_at)
) ENGINE=InnoDB;

-- 本地消息表 outbox（与业务同事务写入）
CREATE TABLE outbox_message (
  id           BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
  msg_id       VARCHAR(64) NOT NULL,                  -- 业务消息唯一ID
  topic        VARCHAR(64) NOT NULL,
  sharding_key VARCHAR(64) NOT NULL,                  -- 通常 = biz_id
  payload      JSON NOT NULL,
  status       ENUM('pending','published','consumed','dead') NOT NULL DEFAULT 'pending',
  retry        INT UNSIGNED NOT NULL DEFAULT 0,
  next_retry_at DATETIME NOT NULL,
  trace_id     VARCHAR(64) NOT NULL,
  created_at   DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  UNIQUE KEY uk_msg (msg_id),
  KEY idx_dispatch (status, next_retry_at),
  KEY idx_topic (topic, created_at)
) ENGINE=InnoDB;

-- RAG 索引版本
CREATE TABLE index_version (
  id          BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
  version     VARCHAR(16) NOT NULL,                   -- v3
  collection  VARCHAR(64) NOT NULL,                   -- aftersales_v3
  doc_count   INT UNSIGNED NOT NULL DEFAULT 0,
  status      ENUM('building','active','deprecated','rolled_back') NOT NULL,
  eval_score  DECIMAL(5,4) NULL,
  created_at  DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  UNIQUE KEY uk_version (version),
  KEY idx_status (status)
) ENGINE=InnoDB;

-- Prompt 注册表
CREATE TABLE prompt_registry (
  id          BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
  prompt_key  VARCHAR(64) NOT NULL,
  version     INT UNSIGNED NOT NULL,
  content     MEDIUMTEXT NOT NULL,
  status      ENUM('draft','published','deprecated') NOT NULL DEFAULT 'draft',
  eval_run_id VARCHAR(64) NULL,
  updated_by  VARCHAR(64) NOT NULL,
  updated_at  DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3) ON UPDATE CURRENT_TIMESTAMP(3),
  UNIQUE KEY uk_key_ver (prompt_key, version),
  KEY idx_published (prompt_key, status)
) ENGINE=InnoDB;

-- 评测用例与运行
CREATE TABLE eval_case (
  id         BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
  case_key   VARCHAR(64) NOT NULL,
  category   ENUM('typical','multi_hop','no_answer','multi_turn') NOT NULL,
  input      JSON NOT NULL,                           -- 问题与前置会话
  expectation JSON NOT NULL,                          -- 期望意图/引用/关键词/答案
  enabled    TINYINT(1) NOT NULL DEFAULT 1,
  UNIQUE KEY uk_case (case_key)
) ENGINE=InnoDB;

CREATE TABLE eval_run (
  id            BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
  run_id        VARCHAR(64) NOT NULL,
  suite         VARCHAR(32) NOT NULL,
  total         INT NOT NULL,
  pass_cnt      INT NOT NULL,
  citation_acc  DECIMAL(5,4) NULL,
  p95_ms        INT NULL,
  report        JSON NOT NULL,                        -- 失败明细与归因
  started_at    DATETIME(3) NOT NULL,
  finished_at   DATETIME(3) NULL,
  UNIQUE KEY uk_run (run_id)
) ENGINE=InnoDB;

-- 工具审计
CREATE TABLE tool_audit (
  id          BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
  trace_id    VARCHAR(64) NOT NULL,
  user_id     VARCHAR(64) NOT NULL,
  tool        VARCHAR(64) NOT NULL,
  level       ENUM('ro','write') NOT NULL,
  params_hash CHAR(64) NOT NULL,
  result_code VARCHAR(32) NOT NULL,
  cost_ms     INT NOT NULL,
  created_at  DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  KEY idx_trace (trace_id),
  KEY idx_user_time (user_id, created_at)
) ENGINE=InnoDB;
```

### 10.2 事务边界

| 操作 | 同步事务内完成 | 异步（MQ 侧）完成 |
| --- | --- | --- |
| 发起售后 | 校验 + INSERT `aftersales_biz` + `transitions` + INSERT `outbox` | 工单创建、通知下发 |
| 审批回调 | `approval_task` 幂等更新 + `aftersales_biz` 状态迁移（乐观锁）+ outbox | 执行器调度、消息通知 |
| 索引切换 | `index_version` 状态流转 | 向量重建（worker） |

> 原则：**事务内不发网络请求**（不发 MQ、不调 LLM）；消息由 dispatcher 轮询 `outbox(status=pending)` 投递，或同进程 after-commit 钩子投递 + 失败回落轮询。

---

## 11. Redis 设计

### 11.1 Key 规范

| Key 模式 | 值 | TTL | 用途 |
| --- | --- | --- | --- |
| `sess:{session_id}` | 会话记忆窗口 JSON | 24h | 多轮上下文（超长转摘要） |
| `ckpt:{session_id}` | LangGraph checkpoint | 24h | 图状态恢复 |
| `rag:{ver}:{query_hash}` | 检索结果 JSON | 300s | 检索缓存（绑索引版本） |
| `faq:{q_hash}` | FAQ 答案 | 30min | 高频问题直答 |
| `fact:sku:{sku_id}` | 商品事实 | 10min | 热点事实（双检锁） |
| `idem:{idem_key}` | 首次结果引用 | 24h | 写接口幂等 |
| `lock:as:{biz_id}` | 线程持锁标记 | 5s + 续期 | 分布式锁（防并发双开单） |
| `confirm:{token}` | confirm_token 载荷 | 5min | 双确认令牌 |
| `rl:{user_id}:{route}` | 计数器 | 1–60s 窗口 | 限流 |
| `cb:write:error:{window}` | 错误计数 | 60s | 熔断统计 |
| `idx:active` | 当前索引版本号 | 永久 | 检索路由 |

### 11.2 关键算法

**分布式锁（开单互斥）**

```
SET lock:as:{biz_id} {uuid} NX PX 5000
业务执行（若 >3s，每 2s Lua 续期）
DEL（仅 value 匹配时 Lua 删除，防误删）
```

**双检防击穿**

```
1) GET 命中 → 返回
2) SETNX lock:fact:{id} EX 5 → 成功者回源
   失败者 50ms 自旋等回源结果（最多 10 次），仍无则返回降级值
3) 回源结果 SETEX 正常 TTL + 本地 Caffeine 缓存 60s（延迟过期消抖）
```

**写接口幂等**

```
入站：SET idem:{idem_key} {resp_ref} NX EX 86400
     已存在 → 比对参数 hash：一致返回原结果 / 不一致返回 AS-4004
执行成功后：以实际响应覆盖 value
```

---

## 12. 消息队列设计（RocketMQ / Kafka）

### 12.1 Topic 清单

| Topic | 生产者 | 消费者 | 语义 | 保留 |
| --- | --- | --- | --- | --- |
| `AS_TICKET_CREATE` | api(outbox) | worker | 创建售后工单 | 3d |
| `AS_APPROVAL_EVENT` | api(outbox) | worker/通知服务 | 审批结果事件 | 3d |
| `AS_NOTIFY` | worker | worker(通知) | 站内信/短信通知 | 1d |
| `AS_EVAL_REPORT` | api | worker | 评测报告异步生成 | 7d |
| `AS_TRACE_LOG` | api/worker | 日志消费者 | trace 明细归档 | 7d |
| `AS_DEAD_LETTER` | broker | admin 告警 | 死信（`%DLQ%{group}`） | 7d |

### 12.2 消息体（信封统一）

```json
{
  "msg_id": "msg_01H...",          
  "biz_id": "AS20261027001",
  "trace_id": "tr_8f3c...",
  "occurred_at": "2026-10-27T09:12:00Z",
  "schema": "as.ticket.create.v1",
  "payload": {"action": "refund", "order_id": "O2026...", "amount": 199.00}
}
```

### 12.3 投递与消费契约

| 项 | 规则 |
| --- | --- |
| 投递 | outbox dispatcher：`SELECT ... WHERE status='pending' AND next_retry_at<=now LIMIT 100`，发布成功置 `published` |
| 重投 | 发布失败指数退避（5s/15s/45s…），`retry>5` 置 `dead` + 发 `AS_DEAD_LETTER` |
| 消费幂等 | 消费者先查 `msg_id` 处理标记（Redis SETNX + DB 唯一键），重复投递直接 ACK |
| 重试 | 消费异常本地重试 3 次 → 回退 broker 重试（延迟等级 10s/30s/1m）→ 死信 |
| 顺序 | 同 `sharding_key`（biz_id）按队列哈希分区，保证单据内事件有序 |
| 削峰 | 消费端并发度 = `min(队列数, 配置阈值)`，保护 MySQL |

---

## 13. 可观测性与评测

### 13.1 Trace 规范

- `trace_id` 由网关生成（或透传 `X-Trace-Id`），长度 16–64，字符集 `[A-Za-z0-9_-]`。
- 传播：HTTP 头 → LangGraph `state.trace_id` → Redis/MQ 消息信封 → MySQL 各表。
- 结构化日志字段（JSON line）：

```json
{"ts":"2026-10-27T09:12:00.123Z","level":"INFO","trace_id":"tr_8f3c","span":"node.retrieve",
 "session_id":"sess_1","user_id":"u_1","event":"rag.hit","cache":false,"top1":0.82,
 "docs":3,"ms":210,"tokens":0,"idx_ver":"v3"}
```

### 13.2 指标（Metrics）

| 指标 | 类型 | 用途 |
| --- | --- | --- |
| `chat_latency_p95` | histogram | 问答 SLA |
| `tool_latency_ms{tool}` | histogram | 工具健康度 |
| `rag_cache_hit_ratio` | gauge | 缓存收益 |
| `llm_tokens_total{node}` | counter | 成本治理 |
| `idem_hit_total` | counter | 重复提交拦截量 |
| `transition_illegal_total` | counter | 非法迁移（应为 0） |
| `outbox_pending` | gauge | 积压告警（>1000 告警） |
| `dlq_total{topic}` | counter | 死信告警 |
| `write_error_ratio` | gauge | 熔断输入 |

### 13.3 评测体系（/eval/run）

| 用例类别 | 数量（MVP） | 断言维度 |
| --- | --- | --- |
| typical（典型问答/办理） | 40 | 意图正确、答案关键词、引用存在 |
| multi_hop（多跳推理） | 15 | 召回 ≥2 篇、事实无冲突 |
| no_answer（无答案/越界） | 15 | 必须兜底或拒绝，禁止编造 |
| multi_turn（多轮指代/澄清） | 20 | 澄清后完成率、改写质量 |

**门禁**：`完成率 ≥ 90%`、`引用正确率 ≥ 95%`、`P95 不劣化 >10%`、`非法迁移 = 0` —— 任一不达标则该版本不可发布（对接 CI）。

---

## 14. 安全设计

| 域 | 措施 |
| --- | --- |
| 鉴权 | JWT（RS256），短时 access + 刷新；角色 `buyer/agent/approver/ops/admin` |
| 注入防御 | 用户输入分隔符包裹 + 系统段「忽略分隔外指令」；工具参数白名单 schema；输出侧关键词+分类器双重检测 |
| 越权 | 写工具校验 `order.user_id == current_user`；审批回调 HMAC 验签 + 内网白名单 |
| 脱敏 | 出口统一脱敏手机号/地址/身份证（正则 + 复核层）；日志中金额以上下文哈希存储 |
| 双确认 | `confirm_token` 单次、5 分钟过期、绑定 `user_id + action + amount` |
| 熔断 | 写链路 60s 窗口错误率 > 50% → 打开熔断 30s（返回 AS-5031，引导人工），半开探测恢复 |
| 限流 | 用户级 20 rpm、单会话并发 1、全局 LLM 并发信号量（可配） |
| 审计 | `tool_audit` + `aftersales_transition` 不可变流水，保留 ≥ 180 天 |

---

## 15. 非功能需求与 SLO

> SLO 是上线验收与日常运维的**唯一标尺**；核心接口 = `POST /chat/stream`、`/aftersales/*`。

| 维度 | SLO / 指标 | 测量与依据 |
| --- | --- | --- |
| 可用性 | 核心接口月度成功率 ≥ **99.9%** | 错误预算 43min/月；**预算耗尽即冻结功能发布，只修故障** |
| 对话延迟 | 首 token P95 ≤ 800ms；端到端 P95 ≤ 1.5s | Prometheus 5min 窗口 `histogram_quantile`，按环境分列 |
| 办事受理 | `/aftersales/apply` P95 ≤ 500ms（不含审批等待） | 同上 |
| 错误率 | 5xx 占比 < 0.1%；状态机非法迁移 = **0** | 非法迁移出现任意一次即 P1 事件 |
| 吞吐容量 | 双副本 api ≥ 100 QPS（问答）/ ≥ 40 TPS（写）；按 3 倍峰值预案扩容 | JMeter 每次大促前压测归档 |
| 降级容错 | LLM/依赖故障 **1min 内**自动熔断降级；只读问答可用、写操作转人工 | 故障注入演练验证 |
| 数据恢复 | **RTO ≤ 30min，RPO ≤ 5min** | 季度脚本化恢复演练实测 |
| 数据保留 | 业务单据永久；trace 明细 90 天；日志 30 天；审计流水 180 天 | 生命周期策略自动清理 |
| 流式兼容 | SSE 断线 30s 内 `session_id + last_event_id` 续传，不重放已发送事件 | 契约测试覆盖 |

---

## 16. 工程规范与目录结构

### 16.1 代码结构

```
aftersales-agent/
├── app/
│   ├── api/                 # 路由：chat / aftersales / approval / eval / traces
│   │   ├── deps.py          # 鉴权、幂等、trace 依赖注入
│   │   └── errors.py        # 统一异常与错误码
│   ├── agent/               # LangGraph 编排
│   │   ├── graph.py         # 图装配、条件边
│   │   ├── nodes/           # router/rewrite/retrieve/answer/tools/hitl...
│   │   ├── state.py         # AfterSalesState
│   │   └── checkpointer.py  # RedisSaver 封装
│   ├── rag/
│   │   ├── chunker.py       # 分块与元数据
│   │   ├── indexer.py       # 离线索引构建
│   │   ├── retriever.py     # 混合召回 + Reranker
│   │   └── cache.py         # 检索缓存与版本联动
│   ├── tools/               # 工具注册（ro / write 分级）
│   ├── services/
│   │   ├── state_machine.py # 唯一状态迁移入口
│   │   ├── idempotency.py   # 幂等键与锁
│   │   ├── outbox.py        # 本地消息表 + dispatcher
│   │   └── approval.py      # 审批任务与回调
│   ├── workers/             # MQ 消费者、索引构建、评测执行
│   ├── eval/                # 用例加载、断言、归因
│   ├── common/              # config/logging/trace/schemas
│   └── main.py              # FastAPI 入口
├── tests/
│   ├── unit/                # 状态机、幂等、分块、schema
│   ├── integration/         # API + MySQL/Redis（testcontainers 或 compose）
│   └── e2e/                 # 完整对话流（录制回放 LLM）
├── evals/cases/             # JSON 用例集（进 git）
├── deploy/
│   ├── docker-compose.yml
│   ├── Dockerfile
│   └── .env.example
├── loadtest/                # JMeter 脚本与断言
└── docs/                    # 本文档与 ADR
```

### 16.2 开发约定

| 项 | 约定 |
| --- | --- |
| 分支 | `feature/*` → `develop` → `main`；PR 必须过 CI 门禁 |
| 提交 | Conventional Commits（`feat(state-machine): ...`） |
| 校验 | `ruff` + `mypy`(strict 关键路径) + `pytest -cov`（核心模块覆盖 ≥ 80%） |
| 配置 | 仅 `pydantic-settings` 读环境变量，禁止硬编码密钥 |
| 日志 | 结构化 JSON；禁止 print；出入参脱敏后记录 |
| ADR | 关键决策（选型、状态机语义）写 `docs/adr/NNNN-*.md` |
| 依赖 | 锁文件进 git；镜像固定小版本 |

### 16.3 交付流水线（CI/CD 门禁）

**合并 main 前（CI，全部通过）**

1. `ruff check` + `mypy`
2. `pytest`（unit + integration）
3. `/eval/run` 回归（门禁阈值见 §13.3）
4. 数据库迁移兼容检查：只允许向前兼容变更（expand），禁止破坏性 DDL 直上
5. 镜像构建 + 漏洞扫描（trivy，CRITICAL=0）+ `docker compose config` 校验
6. 变更涉及 `services/state_machine.py` 时强制状态机全量迁移用例

**发布到生产前（CD，全部通过）**

1. staging 全量评测 + JMeter 冒烟（P95 不劣化 > 10% 即失败）
2. 制品按 digest 部署（CI 签名，**同制品晋级**，禁止 prod 现场构建）
3. 灰度 10% → 观察 ≥30min → 50% → 100%；触发停止条件自动回滚（§17.3）
4. 回滚演练记录随版本归档；发布与回滚全程可追溯到 `trace_id` / 变更单

---

## 17. 生产部署与配置

### 17.1 生产拓扑与高可用

| 层 | 生产要求 |
| --- | --- |
| 应用层 | `api` ≥ 2 副本跨节点（LB 健康检查自动摘流）、`worker` ≥ 2 副本消费分组水平扩展；全部无状态（会话/checkpoint 在 Redis） |
| 数据层选型优先级 | **云托管（RDS 半同步主从 / 云 Redis 哨兵 / 云 MQ 主从）> 自建容器编排** |
| MySQL | 主从半同步 + 自动/一键切换；连接走读写代理或 VIP；`innodb_flush_log_at_trx_commit=1` |
| Redis | 主从 + 三节点哨兵；AOF everysec；缓存全丢可接受，配限流保护 + 自动预热脚本 |
| RocketMQ | Broker 主从同步双写；消费组多实例分摊；死信队列接告警 |
| Chroma | 数据卷每日快照上传对象存储；**权威源为文档对象 + `index_version` 表**，任意时刻可全量重建 |
| 网络 | 外部仅暴露 LB（HTTPS/TLS1.2+）；应用与数据层内网隔离；管理接口限内网/IP 白名单 |

拓扑图见 §2.3；开发/测试使用 `deploy/docker-compose.yml` 单机全量（同镜像、不同副本数）。

### 17.2 环境与制品晋级

| 环境 | 用途 | 说明 |
| --- | --- | --- |
| dev | 日常开发 | compose 单机全量，`LLM_MOCK=1`，mock provider |
| staging | 发布预演 | **与生产同拓扑缩容**（含主从依赖）；跑完整评测 + 压测 + 灰度/回滚演练 |
| prod | 生产 | secrets 注入；api/worker 双副本；只接受 CI 签名的 digest 制品 |

**晋级规则**：同一镜像 digest 由 dev → staging → prod 晋级，禁止在 prod 环境构建；环境差异只允许通过环境变量/secrets 表达；LLM 网关与配额按环境隔离。

### 17.3 发布、灰度与回滚

**发布步骤（SRE checklist）**

1. **迁移先行**：`alembic upgrade head` 只做向前兼容变更（expand），旧代码可继续运行；contract（删列/收紧）延后到下个版本。
2. **滚动替换**：`maxUnavailable=0, maxSurge=1`；`/readyz` 通过才接流量；SSE 长连接先摘流、30s 排空（drain）。
3. **灰度放量**：按 `hash(user_id) % 100 < gray_percent` 分流，10% → 50% → 100%，每档观察 ≥ 30min。
4. **观察指标**：5xx 率、P95、`transition_illegal_total`、DLQ 增长、LLM 错误率、评测抽样跑批。

**自动停止条件**（任一触发立即回滚）

- 5xx > 1% 持续 5min
- P95 恶化 > 30% 持续 10min
- 出现任意一次状态机非法迁移
- DLQ 持续增长且非业务高峰

**回滚（目标 ≤ 15min）**

| 对象 | 手段 |
| --- | --- |
| 应用 | 上一 digest 镜像滚动回退（编排层一键回滚） |
| Prompt | `/admin/prompts/{key}/publish` 指回上一 `published` 版本 |
| 索引 | `/admin/index/{ver}/activate` 回切旧版本（缓存随版本号自动失效） |
| 数据库 | **只回滚代码不回滚 schema**；破坏性变更经 expand-contract 永不触达旧版本 |

### 17.4 备份与容灾

| 数据 | 策略 | RPO / RTO | 演练 |
| --- | --- | --- | --- |
| MySQL | 每日全备 + binlog 持续归档，异地保留 30 天 | ≤ 5min / ≤ 30min | 每季度脚本化恢复演练并归档记录 |
| Redis | AOF everysec + 每日 RDB 上传对象存储 | ≤ 1min / ≤ 10min | 缓存全丢预案：限流保护 + 自动预热 |
| Chroma | 每日快照 + 源文档全量重建能力 | ≤ 24h / ≤ 1h（重建） | 每次索引发布验证重建脚本 |
| 配置/密钥 | secrets 版本化、KMS 加密、90 天轮换 | — | 密钥轮换 runbook |

**故障演练（每季度）**：kill 单副本 api、MySQL 主切换、Redis 哨兵切换、MQ 不可用 10min、LLM 网关故障——全部按 §19 Runbook 执行并计时验收。

### 17.5 关键环境变量（`.env.example`）

```bash
APP_ENV=prod
RELEASE_CHANNEL=prod
JWT_PUBLIC_KEY_PATH=/run/secrets/jwt_pub
DATABASE_URL=mysql+aiomysql://user:pass@mysql:3306/aftersales
REDIS_URL=redis://redis:6379/0
CHROMA_URL=http://chroma:8100
MQ_NAMESERVER=rocketmq:9876
LLM_MODEL=gpt-4o-mini
LLM_FALLBACK_MODEL=internal-model-b     # 主模型故障自动切换
LLM_BASE_URL=https://llm-gateway/v1
LLM_API_KEY_FILE=/run/secrets/llm_key
EMBEDDING_MODEL=text-embedding-3-small
RAG_TOP1_THRESHOLD=0.75
RAG_LOW_THRESHOLD=0.55
APPROVAL_THRESHOLD_CNY=500
WRITE_ERROR_BREAKER_RATIO=0.5
BREAKER_WINDOW_SEC=60
IDLE_APPROVAL_EXPIRE_HOURS=24
GRAY_PERCENT=0
ALERT_WEBHOOK_URL_FILE=/run/secrets/alert_webhook
LOG_LEVEL=INFO
```

### 17.6 服务清单与启动顺序（dev/staging compose）

| 服务 | 镜像/入口 | 端口 | 健康检查 |
| --- | --- | --- | --- |
| api | `app@sha256:...` → `uvicorn app.main:app` | 8000 | `GET /healthz`（存活）/ `GET /readyz`（依赖+迁移版本） |
| worker | 同镜像 → `python -m app.workers.runner` | — | 心跳写 Redis |
| mysql | `mysql:8.0`（生产：主从） | 3306 | `mysqladmin ping` |
| redis | `redis:7`（生产：哨兵） | 6379 | `redis-cli ping` |
| chroma | `chromadb/chroma` | 8100 | `GET /api/v1/heartbeat` |
| rocketmq | `apache/rocketmq`（生产：主从） | 9876/10911 | 启动脚本探活 |
| nginx/LB | `nginx:alpine` / 云 LB | 80/443 | `curl localhost` |
| prometheus/grafana/loki | 可观测栈 | 9090/3000/3100 | 各自 `/healthy` |

启动顺序：依赖组件健康 → `api`（先 `alembic upgrade head`）→ `worker` → `nginx`；关闭顺序相反，SSE 先 drain。

---

## 18. 运营与管理能力

> 原则：**上线后的一切发布与开关都必须在线可操作、可审计、可回滚**，不依赖登录服务器改配置。

| 能力 | 实现 | 要求 |
| --- | --- | --- |
| Prompt 发布/回滚 | `POST /admin/prompts/{key}/publish` | 关联评测门禁，未达标禁止发布；变更审计 |
| 索引版本切换 | `POST /admin/index/{ver}/activate` | 自动联动缓存失效；支持一键回切 |
| 熔断开关 | `POST /admin/breaker` | 秒级生效；打开时业务话术自动转人工 |
| 灰度控制 | `POST /admin/gray` | 0–100 实时生效，无需重启 |
| 运营看板 | `GET /admin/dashboard` | 业务：解决率/转人工率/意图分布；工程：P95/错误率/缓存命中/积压/LLM 成本 |
| 人工接管 | `/admin/sessions/{id}/takeover` | 兜底、熔断、超时会话进人工队列；客服接起后 Agent 挂起 |
| 变更审计 | `GET /admin/audit` | 谁、何时、改了什么、前后值；保留 ≥ 180 天 |
| 转人工通道 | `AS_NOTIFY` + 工单 | 触发条件：低置信兜底、熔断、审批超时、用户主动要求 |

---

## 19. 监控告警与值班（Runbook 索引）

### 19.1 告警规则

| 告警 | 条件 | 级别 | 首选动作（Runbook） |
| --- | --- | --- | --- |
| API 错误率 | 5xx 率 > 1% 持续 5min | **P1** | 查依赖健康 → 灰度回滚；通知 on-call |
| 延迟恶化 | 核心接口 P95 恶化 > 30% 持续 10min | P2 | 查缓存命中率、LLM 网关耗时、下游 RT |
| 状态机非法迁移 | `transition_illegal_total > 0` | **P1** | 冻结写链路，按 trace_id 取证，数据订正 |
| outbox 积压 | `outbox_pending > 1000` 持续 5min | P2 | 扩消费并发、查 MQ/DB、必要时扩容 worker |
| 死信增长 | `dlq_total` 增长 | P2 | 死信重投 runbook + 业务影响评估 |
| MySQL 主从延迟 | 延迟 > 10s | P1 | 切换读流量到主库，排查慢查询 |
| Redis 内存/故障 | 内存 > 80% 或主从切换 | P2/P1 | 热 key 分析、淘汰策略核对、预热 |
| LLM 网关异常 | 错误率 > 10% 或超时 | P1 | 自动切 `LLM_FALLBACK_MODEL`，失败则熔断转人工 |
| 写链路熔断 | 熔断器打开 | P2 | 确认降级话术生效，排障后半开恢复 |
| SLO 错误预算 | 预算消耗速度 > 2×/天 | P2 | 冻结功能发布，集中修复 |

### 19.2 值班与升级

- **值班**：工作日 9:00–22:00 双人 on-call；P1 全天电话升级（5min 未确认升级至备份）。
- **事件流程**：告警 → 15min 内响应 → 按 Runbook 处置 → 24h 内故障报告（时间线、根因、改进项）。
- **日志与追踪**：结构化日志入 Loki 保留 30 天；trace 明细 90 天；审计 180 天；告警必须携带 `runbook_url`。
- **季度演练**：故障注入（§17.4）+ 告警触达实测，演练结果计入 Go-Live 检查清单。

---

---

## 20. 里程碑计划

| 里程碑 | 周期 | 交付物 | 出口标准 |
| --- | --- | --- | --- |
| M0 骨架 | 第 1 周 | 工程脚手架、compose、trace/统一异常、`/healthz`+`/readyz` | CI 绿灯，一键起停 |
| M1 问答链路 | 第 2–3 周 | RAG 索引+召回+引用作答、SSE、会话与缓存 | 典型用例完成率 ≥ 85% |
| M2 办事链路 | 第 4–5 周 | 工具层、状态机、幂等、outbox、HITL 审批 | 幂等/迁移用例全过；端到端办单成功 |
| M3 生产化 | 第 6–7 周 | 监控告警接入、灰度与回滚机制、备份恢复、staging 同拓扑、压测 | §15 SLO 全部达标；回滚实测 ≤ 15min；告警触达实测 |
| **M4 上线** | 第 8 周 | 真实接口联调、安全评审、故障演练、灰度 5%→20%→100% 放量 | **§22 Go-Live 检查清单全部勾选** |

---

## 21. 风险与开放问题

| # | 风险/问题 | 影响 | 缓解 |
| --- | --- | --- | --- |
| R1 | LLM 输出不稳定导致意图误判 | 走错链路 | 结构化输出 + 规则兜底 + 评测集盯防 |
| R2 | 检索阈值过松产生幻觉引用 | 答错政策 | 双阈值 + 无引用不直出 + no_answer 用例 |
| R3 | 写操作并发/重放 | 重复开单 | 幂等键 + 分布式锁 + 乐观锁三层防护 |
| R4 | MQ 积压导致通知延迟 | 体验下降 | `outbox_pending` 告警 + 消费并发可调 + 削峰预案 |
| R5 | **单一 LLM 供应商故障/限流** | 问答不可用 | `LLM_FALLBACK_MODEL` 主备自动切换 + 熔断转人工（P1 告警） |
| R6 | 云依赖配额耗尽或区域故障 | 局部不可用 | 托管依赖开多可用区、容量水位告警、3 倍峰值预案 |
| R7 | **合规与隐私（个保法）** | 法律风险 | 数据清单与脱敏验收、隐私政策、最小化采集、删除权接口（M4 前完成评审） |
| Q1 | **真实订单/物流接口尚未联调** | **上线阻塞** | Provider 契约测试 + staging 联调完成是 M4 出口硬条件 |
| Q2 | Embedding/LLM 选型未定 | 成本与延迟 | 配置化切换，staging 评测集对比后定型 |
| Q3 | 客服工作台前端 | 范围 | 本期管理 API + 工单兜底；Web 前端进二期 |

---

## 22. 上线 Go-Live 检查清单

> M4 出口硬条件：**全部勾选方可 100% 放量**；任一未勾选则维持灰度或回滚。

**联调与数据**

- [ ] 真实订单/物流/退款 Provider 契约测试通过，staging 全链路联调完成
- [ ] 与上游确认限流配额、超时约定、故障降级语义（书面 SLA/阈值）
- [ ] 生产库迁移脚本在 staging 全量演练；expand-contract 策略确认

**安全与合规**

- [ ] TLS1.2+ 全站、WAF/限流生效；越权测试（IDOR）通过
- [ ] Prompt 注入红队用例通过；出口脱敏抽检合格
- [ ] 密钥/KMS 就位且轮换机制验证；管理接口内网白名单生效
- [ ] 个保法合规：数据清单、隐私政策更新、用户数据删除/导出接口就绪（R7 关闭）

**数据与容灾**

- [ ] MySQL 备份恢复演练达标（RTO ≤ 30min / RPO ≤ 5min）
- [ ] Redis / Chroma 丢失预案（预热、重建）实测
- [ ] 时钟同步、日志与审计留存策略生效

**发布与可观测**

- [ ] 灰度与回滚在 staging 实测 ≤ 15min（含 Prompt/索引/镜像三类）
- [ ] 全部告警接入值班通道，触达实测 5min 内；Runbook 链接可用
- [ ] SLO 看板上线（可用性/P95/错误预算/积压/成本）

**质量与演练**

- [ ] 评测门禁连续 3 天全绿；压测达标（§15 吞吐与 P95）
- [ ] 故障演练完成：kill api、MySQL 切换、MQ 故障、LLM 网关故障
- [ ] 非法状态迁移、死信、熔断三类告警实测触发与恢复

**业务运营**

- [ ] 人工队列与客服排班就绪；转人工 P95 ≤ 30s 验证
- [ ] 熔断/降级话术与客服口径对齐；错误码-话术映射表发布
- [ ] 灰度计划与回滚决策人明确（5% → 20% → 100% 各档观察责任人）

---

## 23. 附录

### 23.1 典型对话示例

> **用户**：我要退上周买的那个蓝牙耳机，质量有问题
> **管家**：找到订单 O2026102588（蓝牙耳机 Pro，¥199）。请确认两点：① 原因「质量问题」② 方式「退款」。确认后我会提交（需审批）。
> **用户**：确认
> **管家**：已受理，单号 AS20261027001，状态「待审批」（超过 ¥500 才需人工，本单预计自动通过）。进度可随时问我。

### 23.2 开发评审检查清单（区别于 §22 上线清单）

- [ ] 状态迁移表与代码迁移函数一一对应
- [ ] 所有写接口具备幂等键与双确认（如适用）
- [ ] SSE 事件协议与前端联调确认
- [ ] 错误码表与前端/客服话术对齐
- [ ] 评测集覆盖四类用例且接入 CI
- [ ] compose 一键启动含健康检查与顺序依赖
- [ ] 密钥全部走环境变量/secret，无硬编码

---

*文档完 · 电商售后管家 **v2.0 生产就绪版** · 2026-10-27*

