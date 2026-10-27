"""配置中心：进程内唯一的环境变量入口（Spec §16.2「仅 pydantic-settings 读环境变量」）。

约束：

- 字段命名与 Spec §17.5 环境变量一一对应（大小写不敏感，``DATABASE_URL`` → ``database_url``）；
- 密钥只允许以 *文件路径* 的形式出现（``JWT_PUBLIC_KEY_PATH`` / ``LLM_API_KEY_FILE``），
  进程启动时才读取内容，避免密钥进入镜像层与日志；
- 非 dev/test 环境使用默认开发密钥或缺少公钥路径时 **启动即失败**（fail-fast）。
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

AppEnv = Literal["dev", "test", "staging", "prod"]
LogFormat = Literal["json", "console"]

DEFAULT_DEV_JWT_SECRET = "dev-only-insecure-hs256-secret-change-me"  # noqa: S105

REPO_ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    """全部运行期配置。

    新增字段必须同步 ``deploy/.env.example`` 与 ``tests/unit/test_config.py``。
    """

    model_config = SettingsConfigDict(
        env_file=(".env", "deploy/.env"),
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

    # ---- LLM / RAG（M1 使用，M0 仅占位）----
    llm_model: str = "gpt-4o-mini"
    llm_fallback_model: str = "internal-model-b"
    llm_base_url: str = "http://127.0.0.1:9999/v1"
    llm_api_key_file: str | None = None
    llm_timeout_ms: int = 8000
    llm_max_concurrency: int = 8
    embedding_model: str = "text-embedding-3-small"
    rag_top1_threshold: float = Field(default=0.75, ge=0, le=1)
    rag_low_threshold: float = Field(default=0.55, ge=0, le=1)
    rag_cache_ttl_sec: int = 300
    faq_cache_ttl_sec: int = 1800
    fact_cache_ttl_sec: int = 600
    session_ttl_hours: int = 24

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
        """读取密钥文件内容；路径为空返回 None，文件缺失抛 FileNotFoundError（fail-fast）。"""
        if not path:
            return None
        candidate = Path(path)
        if not candidate.is_absolute():
            candidate = REPO_ROOT / candidate
        return candidate.read_text(encoding="utf-8").strip()

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
