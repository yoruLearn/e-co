"""指标定义（Spec §13.2）。

命名与 Spec 表格保持一致，便于 Grafana 看板与告警规则直接引用：

=====================  ===========  =====================================
指标                    类型         用途
=====================  ===========  =====================================
``chat_latency_seconds`` histogram    问答 SLA（对应 §13.2 chat_latency_p95）
``tool_latency_ms``      histogram    工具健康度
``rag_cache_hit_ratio``  gauge        缓存收益
``llm_tokens_total``     counter      成本治理
``idem_hit_total``       counter      重复提交拦截量
``transition_illegal_total`` counter  非法迁移（应为 0）
``outbox_pending``       gauge        outbox 积压
``dlq_total``            counter      死信
``write_error_ratio``    gauge        熔断输入
``http_requests_total``  counter      RED 指标（M0 接入）
``http_request_duration_seconds`` histogram  RED 指标（M0 接入）
=====================  ===========  =====================================

M0 只有 HTTP 指标真正被写入；业务指标已注册，M1/M2 起由各节点上报。
"""

from __future__ import annotations

from prometheus_client import Counter, Gauge, Histogram

HTTP_REQUESTS = Counter(
    "http_requests_total",
    "HTTP 请求总数",
    ["method", "path", "status"],
)

HTTP_LATENCY = Histogram(
    "http_request_duration_seconds",
    "HTTP 请求耗时",
    ["method", "path"],
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 0.8, 1.0, 1.5, 2.5, 5.0, 10.0),
)

CHAT_LATENCY = Histogram(
    "chat_latency_seconds",
    "对话接口端到端耗时（P95 由 histogram_quantile 计算）",
    buckets=(0.2, 0.5, 0.8, 1.0, 1.5, 2.0, 3.0, 5.0, 8.0),
)

TOOL_LATENCY = Histogram(
    "tool_latency_ms",
    "工具调用耗时（毫秒）",
    ["tool"],
    buckets=(50, 100, 200, 400, 500, 800, 1200, 2000, 5000),
)

RAG_CACHE_HIT_RATIO = Gauge("rag_cache_hit_ratio", "检索缓存命中率")

LLM_TOKENS = Counter("llm_tokens_total", "LLM token 消耗", ["node"])

IDEM_HIT = Counter("idem_hit_total", "幂等命中（重复提交拦截）次数")

TRANSITION_ILLEGAL = Counter("transition_illegal_total", "状态机非法迁移次数（应为 0）")

OUTBOX_PENDING = Gauge("outbox_pending", "outbox 待投递消息数")

DLQ_TOTAL = Counter("dlq_total", "死信数量", ["topic"])

WRITE_ERROR_RATIO = Gauge("write_error_ratio", "写链路错误率（熔断输入）")

APP_INFO = Gauge(
    "app_info",
    "应用版本信息",
    ["version", "env", "release_channel"],
)
