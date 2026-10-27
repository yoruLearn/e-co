"""配置中心测试：字段完整性、校验规则、密钥读取。"""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from app.common.config import (
    DEFAULT_DEV_JWT_SECRET,
    REPO_ROOT,
    Settings,
    get_settings,
)

ENV_EXAMPLE = REPO_ROOT / "deploy" / ".env.example"


def _env_keys() -> set[str]:
    keys: set[str] = set()
    for raw in ENV_EXAMPLE.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        keys.add(line.split("=", 1)[0].strip())
    return keys


def test_env_example_covers_every_settings_field() -> None:
    """环境变量模板必须与 Settings 字段一一对应（否则部署时静默丢配置）。"""
    keys = _env_keys()
    missing = sorted(name.upper() for name in Settings.model_fields if name.upper() not in keys)
    assert missing == [], f"deploy/.env.example 缺少字段: {missing}"


def test_env_example_has_no_unknown_settings_like_keys() -> None:
    """模板里多出的键只允许是 compose 专用的大写下划线变量。"""
    known = {name.upper() for name in Settings.model_fields}
    compose_only = {
        "API_PORT",
        "NGINX_PORT",
        "MYSQL_PORT",
        "MYSQL_ROOT_PASSWORD",
        "MYSQL_DATABASE",
        "MYSQL_USER",
        "MYSQL_PASSWORD",
        "REDIS_PORT",
        "CHROMA_PORT",
        "PROMETHEUS_PORT",
        "ALERTMANAGER_PORT",
        "GRAFANA_PORT",
        "GRAFANA_USER",
        "GRAFANA_PASSWORD",
        "LOKI_PORT",
        "MQ_NAMESRV_PORT",
        "MQ_BROKER_PORT",
    }
    unknown = sorted(_env_keys() - known - compose_only)
    assert unknown == [], f"deploy/.env.example 出现未定义键: {unknown}"


def test_defaults_are_dev_safe() -> None:
    settings = Settings(_env_file=None)
    assert settings.app_env == "dev"
    assert settings.api_prefix == "/api/v1"
    assert settings.dev_login_allowed is True
    assert settings.rag_low_threshold < settings.rag_top1_threshold
    assert settings.approval_threshold_cny == 500


def test_threshold_order_is_enforced() -> None:
    with pytest.raises(ValidationError, match="RAG_LOW_THRESHOLD"):
        Settings(_env_file=None, rag_top1_threshold=0.5, rag_low_threshold=0.7)


def test_gray_percent_range_is_enforced() -> None:
    with pytest.raises(ValidationError):
        Settings(_env_file=None, gray_percent=101)


def test_prod_rejects_default_dev_secret(tmp_path: Path) -> None:
    with pytest.raises(ValidationError, match="默认开发 JWT 密钥"):
        Settings(
            _env_file=None,
            app_env="prod",
            jwt_public_key_path=str(tmp_path / "jwt.pem"),
        )


def test_prod_requires_public_key_path() -> None:
    with pytest.raises(ValidationError, match="JWT_PUBLIC_KEY_PATH"):
        Settings(
            _env_file=None,
            app_env="staging",
            jwt_hs256_secret="a-real-secret-value",
        )


def test_prod_accepts_rs256_without_dev_secret() -> None:
    settings = Settings(
        _env_file=None,
        app_env="prod",
        jwt_algorithm="RS256",
        jwt_hs256_secret="another-real-secret",
        jwt_public_key_path="/run/secrets/jwt_pub",
    )
    assert settings.is_prod is True
    assert settings.dev_login_allowed is False


def test_log_level_normalization_and_validation() -> None:
    assert Settings(_env_file=None, log_level="debug").log_level == "DEBUG"
    with pytest.raises(ValidationError):
        Settings(_env_file=None, log_level="VERBOSE")


def test_mq_endpoint_parsing() -> None:
    assert Settings(_env_file=None, mq_nameserver="rocketmq:9876").mq_host == "rocketmq"
    assert Settings(_env_file=None, mq_nameserver="rocketmq:9876").mq_port == 9876
    assert Settings(_env_file=None, mq_nameserver="rocketmq").mq_port == 9876


def test_read_secret_file(tmp_path: Path) -> None:
    secret_file = tmp_path / "key.txt"
    secret_file.write_text("  top-secret\n", encoding="utf-8")
    assert Settings.read_secret_file(secret_file) == "top-secret"
    assert Settings.read_secret_file(None) is None
    with pytest.raises(FileNotFoundError):
        Settings.read_secret_file(tmp_path / "missing.txt")


def test_settings_are_cached_until_reload() -> None:
    first = get_settings()
    second = get_settings()
    assert first is second


def test_default_dev_secret_constant_is_marked_insecure() -> None:
    assert "insecure" in DEFAULT_DEV_JWT_SECRET
