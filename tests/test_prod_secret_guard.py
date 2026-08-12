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
    }
    values.update(overrides)
    return Settings(**values)


def test_placeholder_api_key_detected() -> None:
    s = _prod_settings(model_gateway_api_key="sk-change-me-to-a-real-key")
    assert "MODEL_GATEWAY_API_KEY" in validate_production_secrets(s)


def test_empty_jwt_key_detected() -> None:
    s = _prod_settings(jwt_signing_key="")
    assert "JWT_SIGNING_KEY" in validate_production_secrets(s)


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


def test_ensure_production_raises_on_violation() -> None:
    s = _prod_settings(model_gateway_api_key="sk-change-me")
    with pytest.raises(RuntimeError, match="MODEL_GATEWAY_API_KEY"):
        ensure_production_secrets_ready(s)


def test_ensure_non_production_is_noop() -> None:
    s = _prod_settings(app_env="local")
    # 即使全是占位符也不抛（非生产不触发）
    ensure_production_secrets_ready(s)
