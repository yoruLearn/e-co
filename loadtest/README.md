# 压测（M3 交付）

M0 只占位目录，原因：Spec §13.3 / §15 的压测门禁（问答 P95 ≤ 1.5s、双副本 ≥100 QPS）
必须有真实对话链路才有意义，M1 之前跑出的数字不可作为基线。

M3 计划：

1. `loadtest/chat.jmx`：JMeter 脚本，SSE 场景用 HTTP 采样器 + 首 token 断言；
2. `loadtest/aftersales.jmx`：写链路（幂等键并发 10 重复提交用例）；
3. `loadtest/assertions.json`：P95 / 错误率阈值，供 CI 对比上一基线（劣化 > 10% 即失败）；
4. 结果归档：`loadtest/results/{date}-{commit}.json`，随发布记录留存。
