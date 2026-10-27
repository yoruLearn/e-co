"""JWT 鉴权与角色测试（Spec §9.1 / §14）。"""

from __future__ import annotations

import jwt
import pytest

from app.api.deps import get_current_principal, get_idempotency_key, require_roles
from app.api.errors import AppError, ErrorCode
from app.common.config import Settings
from app.common.security import Principal, Role, create_access_token, decode_access_token


@pytest.fixture()
def settings() -> Settings:
    return Settings(
        _env_file=None,
        app_env="test",
        # 32 字节以上，避免 PyJWT 的 InsecureKeyLengthWarning 污染测试输出
        jwt_hs256_secret="unit-test-secret-0123456789abcdef",
        jwt_ttl_seconds=600,
    )


def test_token_roundtrip(settings: Settings) -> None:
    token, ttl = create_access_token(user_id="u_1", role=Role.AGENT, settings=settings)
    assert ttl == 600
    principal = decode_access_token(token, settings)
    assert principal == Principal(user_id="u_1", role=Role.AGENT)


def test_claims_contain_spec_fields(settings: Settings) -> None:
    token, _ = create_access_token(user_id="u_1", role=Role.BUYER, settings=settings)
    claims = jwt.decode(token, options={"verify_signature": False})
    assert set(claims) >= {"sub", "role", "iss", "iat", "exp"}
    assert claims["iss"] == settings.jwt_issuer
    assert claims["role"] == "buyer"


def test_expired_token_is_rejected(settings: Settings) -> None:
    token, _ = create_access_token(
        user_id="u_1", role=Role.BUYER, settings=settings, ttl_seconds=-1
    )
    with pytest.raises(AppError) as excinfo:
        decode_access_token(token, settings)
    assert str(excinfo.value.code) == "AS-4010"
    assert "过期" in excinfo.value.message


def test_wrong_secret_is_rejected(settings: Settings) -> None:
    token, _ = create_access_token(user_id="u_1", role=Role.BUYER, settings=settings)
    other = Settings(
        _env_file=None,
        app_env="test",
        jwt_hs256_secret="another-secret-0123456789abcdef",
    )
    with pytest.raises(AppError) as excinfo:
        decode_access_token(token, other)
    assert str(excinfo.value.code) == "AS-4010"


def test_missing_role_claim_is_rejected(settings: Settings) -> None:
    token = jwt.encode(
        {"sub": "u_1", "iss": settings.jwt_issuer, "exp": 9_999_999_999},
        "unit-test-secret-0123456789abcdef",
        algorithm="HS256",
    )
    with pytest.raises(AppError, match="role"):
        decode_access_token(token, settings)


def test_unknown_role_is_rejected(settings: Settings) -> None:
    token = jwt.encode(
        {"sub": "u_1", "role": "root", "iss": settings.jwt_issuer, "exp": 9_999_999_999},
        "unit-test-secret-0123456789abcdef",
        algorithm="HS256",
    )
    with pytest.raises(AppError, match="未知角色"):
        decode_access_token(token, settings)


def test_missing_subject_is_rejected(settings: Settings) -> None:
    token = jwt.encode(
        {"role": "buyer", "iss": settings.jwt_issuer, "exp": 9_999_999_999},
        "unit-test-secret-0123456789abcdef",
        algorithm="HS256",
    )
    with pytest.raises(AppError) as excinfo:
        decode_access_token(token, settings)
    assert str(excinfo.value.code) == "AS-4010"
    assert "令牌无效" in excinfo.value.message


def test_missing_bearer_raises_401(settings: Settings) -> None:
    with pytest.raises(AppError) as excinfo:
        get_current_principal(None, settings)
    assert str(excinfo.value.code) == "AS-4010"


def test_require_roles_grants_allowed_role() -> None:
    dependency = require_roles(Role.OPS, Role.ADMIN)
    principal = Principal(user_id="u_1", role=Role.ADMIN)
    assert dependency(principal) == principal


def test_require_roles_denies_other_role() -> None:
    dependency = require_roles(Role.ADMIN)
    with pytest.raises(AppError) as excinfo:
        dependency(Principal(user_id="u_1", role=Role.BUYER))
    assert str(excinfo.value.code) == "AS-4030"
    assert excinfo.value.detail["actual_role"] == "buyer"


def test_rs256_without_key_file_fails_closed() -> None:
    settings = Settings(
        _env_file=None,
        app_env="test",
        jwt_algorithm="RS256",
        jwt_public_key_path=None,
        jwt_private_key_path=None,
    )
    with pytest.raises(AppError) as excinfo:
        decode_access_token("any.token.value", settings)
    assert str(excinfo.value.code) == str(ErrorCode.INTERNAL)


@pytest.mark.parametrize(
    ("header", "expected"),
    [
        (None, None),
        ("AS20261027001:create", "AS20261027001:create"),
        ("short", None),
        ("bad key with spaces", None),
    ],
)
def test_idempotency_header_parsing(header: str | None, expected: str | None) -> None:
    if expected is not None:
        assert get_idempotency_key(header) == expected
    elif header is None:
        assert get_idempotency_key(None) is None
    else:
        with pytest.raises(AppError) as excinfo:
            get_idempotency_key(header)
        assert str(excinfo.value.code) == "AS-4001"


def test_principal_role_helpers() -> None:
    principal = Principal(user_id="u", role=Role.AGENT)
    assert principal.has_any_role((Role.AGENT, Role.ADMIN)) is True
    assert principal.has_any_role((Role.BUYER,)) is False


def test_roles_match_spec() -> None:
    assert {role.value for role in Role} == {"buyer", "agent", "approver", "ops", "admin"}
