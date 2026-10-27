# ADR-0002 · LangGraph 与 Checkpointer 选型（M1-00 spike 结论）

| 项 | 内容 |
| --- | --- |
| 状态 | 已接受 |
| 日期 | 2026-10-27 |
| 范围 | M1（问答链路图）、M2（审批挂起恢复） |
| 依据 | Spec §2.2（Agent 编排选型）、§6（图结构/节点规范）、§6.2 第 5 条、FR-AS-06 |
| 结论 | 采用 **langgraph 1.2.x + langgraph-checkpoint-redis 0.5.x**；Redis TTL 用**分钟**；`asetup()` 必须在首次使用前 await；测试默认 `InMemorySaver` |

---

## 1. 背景

Spec §6 要求用 LangGraph 表达「意图路由 → 改写 → 检索 → 作答 / 澄清 / 拒绝」的条件图，
并在 §6.2 第 5 条要求 `RedisSaver`（会话级、TTL 24h）支撑审批挂起跨进程恢复（FR-AS-06）。

M0 只锁定了节点名、超时与裁剪常量（`app/agent/graph.py`），没有引入依赖。
M1-00 的任务是：**在 Python 3.11 + pydantic v2 环境下验证这套 API 真能用**，再决定是否照此实现。

## 2. 实测结果（2026-10-27）

| 验证项 | 结果 |
| --- | --- |
| `langgraph` 版本 | 1.2.18（`uv pip install` 解析无冲突） |
| `langgraph-checkpoint-redis` | 0.5.2（连带 `langgraph-checkpoint 4.3.1`、`redisvl 0.28`、`numpy 2.4`） |
| `StateGraph` + TypedDict 状态 | ✅ 节点/条件边/END 正常，`invoke` 返回合并后的 state |
| `InMemorySaver` | ✅ `get_state(config)` 可读回 checkpoint，`next=()` 表示图已结束 |
| `AsyncRedisSaver` 构造 | ✅ 只构造**不连接** Redis（`redis_url` 不存在的端口也不报错） |
| `AsyncRedisSaver` TTL 参数 | `ttl: Optional[Dict[str, Any]]`，键为 `default_ttl` / `refresh_on_read` |
| **TTL 单位** | **分钟**（0.5.x 内部按分钟换算），不是秒 —— 这是最容易被写错的一处 |
| `asetup()` | 异步、返回 None、负责建索引；必须在使用前 await |
| 跨实例恢复 | CI 用两个独立 saver 实例（模拟两个进程）验证同一 `thread_id` 能读回 state |

## 3. 决策

### 3.1 直接采用 LangGraph，不写"等价内部图执行器"

spike 没有发现与 3.11 / pydantic v2 / asyncio 的冲突，Spec 指定的技术栈可用，
因此不引入自研图执行器（M1 计划中的"回退方案"不启用）。

### 3.2 Checkpointer 双后端

| 后端 | 使用场景 | 行为 |
| --- | --- | --- |
| `memory`（默认） | 单测、无 Redis 的本地开发 | 进程内，重启即丢；图机制一致 |
| `redis` | staging/prod、CI 集成测试 | 跨进程/跨实例恢复，TTL = `SESSION_TTL_HOURS * 60` 分钟，`refresh_on_read=true` |

好处：CI 不需要为了跑图而先起 Redis；生产仍满足"审批挂起可恢复"。

### 3.3 图与存储解耦

节点只读写 `AfterSalesState`，不直接碰 Redis；checkpointer 由 `build_checkpointer(settings)` 注入，
`init_checkpointer()` / `close_checkpointer()` 负责生命周期。
这样 M2 的 `approval` 节点挂起/恢复无需改图结构。

### 3.4 可观测性

- 节点级 span 写入 `trace_span`（M1-11），不依赖 LangGraph 自带 tracing；
- 显式关闭第三方上报（`LANGCHAIN_TRACING_V2=false`），避免 prompt/数据出境（Spec §14）。

## 4. 代价与风险

| 项 | 说明 | 缓解 |
| --- | --- | --- |
| 依赖体积 | 新增约 20 个包（含 numpy、redisvl、langsmith） | 只在主依赖装一次；CI 缓存 |
| API 漂移 | 1.x → 2.x 可能调整 `BaseCheckpointSaver` 泛型 | 图与 saver 只在 `graph.py`/`checkpointer.py` 各一处，改动面收敛 |
| TTL 单位陷阱 | 秒写成分钟会变成 24h → 1440h | 统一走 `CheckpointPolicy.ttl_minutes`，单测锁定 |
| Redis 不可用 | checkpointer 读写失败会中断对话 | M1-09 的降级：checkpoint 失败时退化为无状态单轮并告警（不阻塞回答） |

## 5. 验证证据

```text
pytest tests/unit/test_checkpointer.py                       9 passed
pytest -m integration_redis tests/integration/test_checkpoint_redis.py
  → CI「RAG 端到端（MySQL 8 + Chroma）」job 的 redis-checkpoint 步骤（真实 Redis 7）
```

代码落点：

- `app/agent/checkpointer.py`：`CheckpointPolicy` / `build_checkpointer` / `init_checkpointer` / `close_checkpointer`
- `app/common/config.py`：`CHECKPOINTER_BACKEND=memory|redis`
- `tests/unit/test_checkpointer.py`、`tests/integration/test_checkpoint_redis.py`
