"""配置中心：进程内唯一的环境变量入口（Spec §16.2「仅 pydantic-settings 读环境变量」）。

约束：

- 字段命名与 Spec §17.5 环境变量一一对应（大小写不敏感，``DATABASE_URL`` → ``database_url``）；
- 密钥只允许以 *文件路径* 的形式出现（``JWT_PUBLIC_KEY_PATH`` / ``LLM_API_KEY_FILE``），
  进程启动时才读取内容，避免密钥进入镜像层与日志；
- 非 dev/test 环境使用默认开发密钥或缺少公钥路径时 **启动即失败**（fail-fast）。
"""

from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

AppEnv = Literal["dev", "test", "staging", "prod"]
LogFormat = Literal["json", "console"]

DEFAULT_DEV_JWT_SECRET = "dev-only-insecure-hs256-secret-change-me"  # noqa: S105

REPO_ROOT = Path(__file__).resolve().parents[2]

_ENV_ASSIGNMENT = re.compile(r"^([A-Z][A-Z0-9_]{1,64})=(.*)$")


def parse_secret_text(text: str) -> str:
    """容错解析密钥文件：纯密钥、env 风格 ``LLM_KEY=sk-...``，或多行 PEM。

    变量名限定全大写，避免把含 ``=`` 的密钥误当赋值；PEM 必须在首行就识别出来，
    否则会被"第一行优先"截断（RS256 曾因此验签失败）。返回值不做日志输出。
    """
    cleaned = text.strip().lstrip("\ufeff")
    if cleaned.startswith("-----BEGIN"):
        return cleaned
    for raw_line in cleaned.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        match = _ENV_ASSIGNMENT.match(line)
        if match:
            return match.group(2).strip().strip('"').strip("'")
        break  # 首行不是赋值 → 整体视为密钥（可能是多行）
    return cleaned.strip('"').strip("'")


class Settings(BaseSettings):
    """全部运行期配置。

    新增字段必须同步 ``deploy/.env.example`` 与 ``tests/unit/test_config.py``。
    """

    model_config = SettingsConfigDict(
        # 只读仓库根 .env：deploy/.env 是给 compose 做变量插值的，不应反向覆盖本地配置
        # （两者叠加曾导致 deploy/.env 里的旧 LLM_MODEL 悄悄盖掉根 .env，实测踩过一次）
        env_file=(".env",),
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
        validate_default=True,
    )

    # ---- 应用 ----
    app_name: str = "aftersales-agent"
    app_version: str = "0.1.0"
    app_env: AppEnv = "dev"
    release_channel: str = "dev"
    api_prefix: str = "/api/v1"
    log_level: str = "INFO"
    log_format: LogFormat = "json"
    cors_allow_origins: list[str] = Field(default_factory=list)
    docs_enabled: bool = True
    shutdown_drain_seconds: int = Field(default=3, ge=0, le=60)

    # ---- 依赖组件（§17.5）----
    database_url: str = (
        "mysql+aiomysql://aftersales:aftersales@127.0.0.1:3306/aftersales?charset=utf8mb4"
    )
    redis_url: str = "redis://127.0.0.1:6379/0"
    chroma_url: str = "http://127.0.0.1:8100"
    mq_enabled: bool = False
    mq_nameserver: str = "127.0.0.1:9876"
    mq_consumer_group: str = "aftersales-dev"
    mq_topic_ticket_create: str = "AS_TICKET_CREATE"

    # ---- 探针 ----
    ready_timeout_ms: int = Field(default=2000, ge=100, le=30_000)

    # ---- 鉴权（§9.1 / §14）----
    jwt_algorithm: Literal["HS256", "RS256"] = "HS256"
    jwt_hs256_secret: SecretStr = SecretStr(DEFAULT_DEV_JWT_SECRET)
    jwt_public_key_path: str | None = None
    jwt_private_key_path: str | None = None
    jwt_issuer: str = "aftersales-agent"
    jwt_ttl_seconds: int = Field(default=3600, ge=60, le=86_400)

    # ---- LLM / RAG（M1）----
    llm_provider: Literal["mock", "openai"] = "mock"
    llm_model: str = "deepseek-flash"
    llm_fallback_model: str = "deepseek-v4-pro"
    llm_base_url: str = "https://api.deepseek.com"
    llm_api_key_file: str | None = None
    llm_timeout_ms: int = 15_000
    llm_max_concurrency: int = 8
    llm_max_output_tokens: int = Field(default=800, ge=16, le=8192)
    llm_reasoning_effort: Literal["low", "high", "max"] | None = "low"
    llm_retry: int = Field(default=1, ge=0, le=3)
    llm_mock_cassette_dir: str = "evals/cassettes"

    embedding_provider: Literal["hash", "fastembed", "api"] = "hash"
    embedding_model: str = "BAAI/bge-small-zh-v1.5"
    embedding_dim: int = Field(default=256, ge=32, le=4096)
    embedding_api_url: str | None = None
    hf_endpoint: str = "https://hf-mirror.com"

    rag_top1_threshold: float = Field(default=0.75, ge=0, le=1)
    rag_low_threshold: float = Field(default=0.55, ge=0, le=1)
    rag_top_k_vector: int = Field(default=20, ge=1, le=200)
    rag_top_k_keyword: int = Field(default=20, ge=1, le=200)
    rag_top_k_fused: int = Field(default=10, ge=1, le=100)
    rag_top_k_final: int = Field(default=5, ge=1, le=50)
    rag_rrf_k: int = Field(default=60, ge=1, le=1000)
    rag_index_version: str = "v1"
    rag_offline: bool = False
    rerank_provider: Literal["none", "llm"] = "none"
    keyword_backend: Literal["mysql", "memory"] = "mysql"
    corpus_dir: str = "seeds/corpus"

    rag_cache_ttl_sec: int = 300
    faq_cache_ttl_sec: int = 1800
    fact_cache_ttl_sec: int = 600
    session_ttl_hours: int = 24
    session_window_turns: int = Field(default=10, ge=2, le=50)
    prompt_cache_ttl_sec: int = 60
    prompt_seed_path: str = "seeds/prompts.yaml"
    # LangGraph checkpointer：memory 用于测试/无 Redis 的本地开发；redis 为生产形态
    checkpointer_backend: Literal["memory", "redis"] = "memory"

    sse_buffer_ttl_sec: int = 300
    sse_buffer_max_frames: int = 500
    trace_span_enabled: bool = True

    # ---- 业务开关（§9 / §17.5）----
    approval_threshold_cny: float = Field(default=500.0, ge=0)
    write_error_breaker_ratio: float = Field(default=0.5, ge=0, le=1)
    breaker_window_sec: int = 60
    idle_approval_expire_hours: int = 24
    gray_percent: int = Field(default=0, ge=0, le=100)
    idempotency_ttl_sec: int = 86_400
    user_rate_limit_per_min: int = 20

    # ---- worker ----
    worker_heartbeat_interval_sec: int = Field(default=10, ge=1)
    worker_heartbeat_ttl_sec: int = Field(default=30, ge=5)

    # ---- 运维 ----
    alert_webhook_url_file: str | None = None

    # ------------------------------------------------------------------ #
    # 校验
    # ------------------------------------------------------------------ #
    @field_validator("log_level")
    @classmethod
    def _normalize_log_level(cls, value: str) -> str:
        level = value.strip().upper()
        allowed = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
        if level not in allowed:
            raise ValueError(f"LOG_LEVEL 必须是 {sorted(allowed)} 之一，当前为 {value!r}")
        return level

    @model_validator(mode="after")
    def _validate_consistency(self) -> Settings:
        if self.rag_low_threshold >= self.rag_top1_threshold:
            raise ValueError(
                "RAG_LOW_THRESHOLD 必须小于 RAG_TOP1_THRESHOLD，"
                f"当前 {self.rag_low_threshold} >= {self.rag_top1_threshold}"
            )
        if self.rag_top_k_final > self.rag_top_k_fused:
            raise ValueError("RAG_TOP_K_FINAL 不能大于 RAG_TOP_K_FUSED")
        if self.rag_top_k_fused > self.rag_top_k_vector + self.rag_top_k_keyword:
            raise ValueError("RAG_TOP_K_FUSED 大于两路召回总量，配置无意义")
        if self.llm_provider == "openai" and self.app_env in ("staging", "prod"):
            if not self.llm_api_key_file:
                raise ValueError("staging/prod 使用真实 LLM 时必须配置 LLM_API_KEY_FILE")
            if self.llm_base_url.startswith("http://127.0.0.1"):
                raise ValueError("staging/prod 不允许指向本地 mock 地址的 LLM_BASE_URL")
        if self.app_env in ("staging", "prod"):
            if self.jwt_hs256_secret.get_secret_value() == DEFAULT_DEV_JWT_SECRET:
                raise ValueError(
                    "staging/prod 禁止使用默认开发 JWT 密钥，请配置 JWT_PUBLIC_KEY_PATH（RS256）"
                )
            if not self.jwt_public_key_path:
                raise ValueError("staging/prod 必须配置 JWT_PUBLIC_KEY_PATH")
        return self

    # ------------------------------------------------------------------ #
    # 派生属性
    # ------------------------------------------------------------------ #
    @property
    def is_prod(self) -> bool:
        return self.app_env == "prod"

    @property
    def dev_login_allowed(self) -> bool:
        """仅 dev/test 允许本地签发令牌；真实登录对接用户中心属 M4。"""
        return self.app_env in ("dev", "test")

    @property
    def mq_host(self) -> str:
        return self.mq_nameserver.rsplit(":", 1)[0]

    @property
    def mq_port(self) -> int:
        raw = self.mq_nameserver.rsplit(":", 1)
        if len(raw) == 2 and raw[1].isdigit():
            return int(raw[1])
        return 9876

    @property
    def migrations_dir(self) -> Path:
        return Path(__file__).resolve().parents[1] / "migrations"

    # ------------------------------------------------------------------ #
    # 密钥读取
    # ------------------------------------------------------------------ #
    @staticmethod
    def read_secret_file(path: str | Path | None) -> str | None:
        """读取密钥文件内容；路径为空返回 None，文件缺失抛 FileNotFoundError（fail-fast）。

        兼容两种常见写法（不打印、不落日志）：

        - 纯密钥：``sk-xxxx``
        - env 风格单行：``LLM_KEY=sk-xxxx``（变量名全大写，避免与含 ``=`` 的密钥冲突）
        """
        if not path:
            return None
        candidate = Path(path)
        if not candidate.is_absolute():
            candidate = REPO_ROOT / candidate
        return parse_secret_text(candidate.read_text(encoding="utf-8"))

    def llm_api_key(self) -> str | None:
        return self.read_secret_file(self.llm_api_key_file)

    def jwt_public_key(self) -> str | None:
        return self.read_secret_file(self.jwt_public_key_path)

    def jwt_private_key(self) -> str | None:
        return self.read_secret_file(self.jwt_private_key_path)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """进程内单例；测试用 ``create_app(settings=...)`` 或 ``reload_settings()`` 切换。"""
    return Settings()


def reload_settings() -> Settings:
    get_settings.cache_clear()
    return get_settings()
