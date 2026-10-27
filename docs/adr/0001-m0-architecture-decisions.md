# ADR-0001 · M0 骨架的六项关键决策

| 项 | 内容 |
| --- | --- |
| 状态 | 已接受 |
| 日期 | 2026-10-27 |
| 范围 | M0（骨架），影响 M1–M4 |
| 依据 | Spec v2.0 §16.1 / §16.2 / §17 / §19 |

---

## 决策 1：Python 固定 3.11.15，依赖用 uv 锁定

**背景**：开发机默认 Python 为 3.14，Spec §2.2 要求 3.11；自由浮动版本会让 CI 与本地行为漂移。

**决策**：`.python-version` 固定 `3.11`；`uv` 生成 `uv.lock` 并入 git；`pyproject.toml` 中 `requires-python = ">=3.11,<3.13"`。

**代价**：不能直接使用 3.13+ 的新语法（如 PEP 695 泛型），团队需按 3.11 语法书写。

**替代方案**：跟随系统 3.14。否决理由：LangChain/LangGraph 生态对 3.14 支持未验证，M1 会踩兼容坑。

---

## 决策 2：探针语义——**启动不连依赖，就绪由 `/readyz` 判定**

**背景**：Spec §9.2 要求 `/readyz` 校验 MySQL/Redis/MQ/Chroma 与迁移版本；§17.6 要求 compose 按依赖健康顺序启动。

**决策**：

- `create_app()` 只构造客户端（engine / redis / httpx），**不发起任何连接**；
- `/healthz` 纯内存计算，永不失败；
- `/readyz` 并发探测 5 项依赖，每项独立超时（默认 2s 总预算），失败返回 503 + 逐项明细；
- 迁移版本不一致 = 未就绪（而不是启动崩溃）。

**理由**：若启动即连库，compose 首次启动时 MySQL 尚未 ready 会导致 api 退出 → 重启循环；k8s 下表现为 CrashLoopBackOff，排障成本高。探针化让"起得来、但暂时不可服务"成为可观测的中间态。

**代价**：需要额外一个探针端点，且 LB 必须配置 `/readyz` 而非只看进程存活。

---

## 决策 3：统一错误信封，新增 `AS-5002` 表示"里程碑未到"

**背景**：M0 的多数业务端点是骨架，若返回 404 或裸 500，前端会误判为"接口不存在/服务崩溃"。

**决策**：

- 所有出口（含 404/422/未捕获异常）统一 `{"code","message","detail","trace_id"}`；
- 新增 `AS-5002 / 501`，`detail` 必带 `milestone`（M1/M2/M3）与 `feature`；
- 未捕获异常一律 `AS-5001`，**堆栈只进日志**，响应体不含任何内部信息。

**代价**：错误码表比 Spec 多一个（已在 `docs/m0/01-requirements-analysis.md` 登记为内部扩展）。

---

## 决策 4：把 Spec 的表格变成代码常量，并用契约测试锁死

**背景**：§5.2 状态迁移表、§8.1 工具清单、§7.2 阈值分档、§12.2 消息信封都是"唯一权威来源"，但只存在于文档里，代码里最容易分叉。

**决策**：

- `TRANSITIONS` / `TOOL_SPECS` 用 frozen dataclass 元组表达，逐行与表格对应；
- 单测以"Spec 原文行列表"为期望值做全量比对，并用笛卡尔积覆盖全部非法迁移；
- ORM 表集合 ↔ 迁移 `create_table` ↔ `EXPECTED_TABLES` 三向比对。

**结果**：M2 接 DB 时，只要 `state_machine.transition()` 复用 `TRANSITIONS`，就不可能"表格说合法、代码说不合法"。

**代价**：改表格时必须同时改代码与测试（这是特性，不是缺陷）。

---

## 决策 5：MQ 在 dev 可选（profile + `MQ_ENABLED` + `skipped` 语义）

**背景**：RocketMQ 单机栈（namesrv + broker）体积大、Windows 桥接网络易踩坑，但 Spec §17.6 要求 dev/staging 单机全量。

**决策**：

- compose 用 `profiles: ["mq"]` 隔离，默认 dev 栈不含 MQ；
- `MQ_ENABLED=false` 时 `/readyz` 把 mq 检查标记 `skipped`，**仍然出现在明细里**；
- 消费者/投递器属 M2，M0 不引入 RocketMQ 客户端依赖。

**理由**：M0 没有任何 MQ 流量，强制拉起只会放大环境噪音；`skipped ≠ ok` 保证不伪装健康。

**代价**：M2 联调 MQ 时需 `-WithMq` 启动，CI 的完整链路验证放到 M2/M3。

---

## 决策 6：鉴权 dev/prod 双模式，**不臆造登录**

**背景**：Spec §9.1 定义 JWT 与角色，但未定义用户源；真实登录对接用户中心属 M4。

**决策**：

- `jwt_algorithm` 可配：dev/test 用 HS256（本地零依赖），staging/prod 强制 RS256 + 公钥文件；
- staging/prod 使用默认开发密钥或缺少公钥路径 → **启动即失败**；
- `POST /auth/login` 仅 dev/test 签发令牌，其它环境返回 `AS-5002`；
- 测试用 `create_access_token` 直接铸币，不依赖登录端点。

**理由**：假登录一旦流入 staging，会把"无鉴权"伪装成"已鉴权"，是安全事故的常见起点。
