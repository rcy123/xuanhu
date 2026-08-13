"""阶段 3 认证测试：生产密钥 fail-fast 守卫（T3.3 / 验收清单）。

- ``APP_ENV=production`` + 占位符/默认值/缺失密钥 → validate 报出变量名。
- ``ensure_production_secrets_ready`` 生产环境抛 RuntimeError；非生产 no-op。
- 合法强随机值通过校验。
"""

from __future__ import annotations

import pytest

from app.core.config import (
    Settings,
    ensure_production_secrets_ready,
    validate_production_secrets,
)


def _prod_settings(**overrides) -> Settings:
    """构造生产环境 Settings（用当前进程 env 覆盖，避免触碰真实 .env）。"""
    values = {
        "app_env": "production",
        "database_url": "postgresql://u:p@localhost:5432/xuanhu",
        "redis_url": "redis://:p@localhost:6379/0",
        "model_gateway_base_url": "http://gw.internal/v1",
        "model_gateway_api_key": "sk-real-strong-key-0123456789abcdef",
        "chat_model": "mimo-7b",
        "embedding_model": "bge-m3",
        "embedding_dim": 1024,
        "jwt_signing_key": "k9s2mF7qW4zX8cV3bN6mJ1pL0tR5yH8uE2gA4sD7fG1hJ3kL5mN7pQ9rS2tU4vW",
        "jwt_signing_key_previous": "",
        "model_gateway_timeout_seconds": 60,
        "xuanhu_prod_secret_guard": True,
        # 字段带 validation_alias=MODEL_WHITELIST，构造时必须用 alias 名。
        "MODEL_WHITELIST": ["mimo-7b", "bge-m3"],
    }
    values.update(overrides)
    return Settings(**values)


def test_placeholder_api_key_detected() -> None:
    s = _prod_settings(model_gateway_api_key="sk-change-me-to-a-real-key")
    assert "MODEL_GATEWAY_API_KEY" in validate_production_secrets(s)


def test_empty_jwt_key_detected() -> None:
    s = _prod_settings(jwt_signing_key="")
    assert "JWT_SIGNING_KEY" in validate_production_secrets(s)


def test_short_jwt_key_detected() -> None:
    """HS256 密钥强度不足（<32 字节）视为不合规，防止弱密钥放行。"""
    s = _prod_settings(jwt_signing_key="too-short-key")
    violations = validate_production_secrets(s)
    assert "JWT_SIGNING_KEY_TOO_SHORT" in violations


def test_strong_jwt_key_not_flagged_for_length() -> None:
    """≥32 字节的强随机 key 不触发长度违规。"""
    s = _prod_settings()  # 默认 64 字符强 key
    assert "JWT_SIGNING_KEY_TOO_SHORT" not in validate_production_secrets(s)


def test_default_middleware_credentials_detected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("POSTGRES_PASSWORD", "xuanhu_dev")
    monkeypatch.setenv("REDIS_PASSWORD", "xuanhu_dev")
    monkeypatch.setenv("MINIO_ACCESS_KEY", "minioadmin")
    monkeypatch.setenv("MINIO_SECRET_KEY", "minioadmin")
    s = _prod_settings()
    violations = validate_production_secrets(s)
    assert "POSTGRES_PASSWORD" in violations
    assert "REDIS_PASSWORD" in violations
    assert "MINIO_ACCESS_KEY" in violations
    assert "MINIO_SECRET_KEY" in violations


def test_strong_secrets_pass() -> None:
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setenv("POSTGRES_PASSWORD", "aT0tallyR4ndom!Passw0rd-2026")
    monkeypatch.setenv("REDIS_PASSWORD", "another-Random-Passw0rd-2026")
    monkeypatch.setenv("MINIO_ACCESS_KEY", "ab12cd34ef56")
    monkeypatch.setenv("MINIO_SECRET_KEY", "super-secret-minio-key-2026")
    try:
        assert validate_production_secrets(_prod_settings()) == []
    finally:
        monkeypatch.undo()


def test_production_empty_model_whitelist_detected() -> None:
    """M5：生产空白名单视为不合规（防止模型名被篡改指向任意端点）。"""
    s = _prod_settings(**{"MODEL_WHITELIST": []})
    assert "MODEL_WHITELIST" in validate_production_secrets(s)
    with pytest.raises(RuntimeError, match="MODEL_WHITELIST"):
        ensure_production_secrets_ready(s)


def test_non_production_empty_whitelist_is_noop() -> None:
    """非生产环境空白名单不触发 fail-fast（local/staging 由 gateway 层校验）。"""
    s = _prod_settings(app_env="staging", **{"MODEL_WHITELIST": []})
    assert "MODEL_WHITELIST" not in validate_production_secrets(s)
    ensure_production_secrets_ready(s)


def test_ensure_production_raises_on_violation() -> None:
    s = _prod_settings(model_gateway_api_key="sk-change-me")
    with pytest.raises(RuntimeError, match="MODEL_GATEWAY_API_KEY"):
        ensure_production_secrets_ready(s)


def test_ensure_non_production_is_noop() -> None:
    s = _prod_settings(app_env="local")
    # 即使全是占位符也不抛（非生产不触发）
    ensure_production_secrets_ready(s)
