"""L0-3 Runtime Feature Flag 契约测试。"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.core.config import Settings


def _settings(monkeypatch: pytest.MonkeyPatch, value: str | None) -> Settings:
    required = {
        "DB_URL": "postgresql://user:pass@localhost/xuanhu",
        "REDIS_URL": "redis://localhost:6379/0",
        "MODEL_GATEWAY_BASE_URL": "http://localhost:8080/v1",
        "MODEL_GATEWAY_API_KEY": "test-only",
        "CHAT_MODEL": "fake-chat",
        "EMBEDDING_MODEL": "fake-embedding",
        "EMBEDDING_DIM": "8",
    }
    for key, item in required.items():
        monkeypatch.setenv(key, item)
    monkeypatch.delenv("AGENT_RUNTIME_ROLLOUT_PHASE", raising=False)
    monkeypatch.delenv("XUANHU_LANGGRAPH_PRODUCT_READY", raising=False)
    if value is None:
        monkeypatch.delenv("AGENT_RUNTIME_VERSION", raising=False)
    else:
        monkeypatch.setenv("AGENT_RUNTIME_VERSION", value)
    return Settings(_env_file=None)  # type: ignore[call-arg]


def test_runtime_flag_defaults_to_legacy(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = _settings(monkeypatch, None)
    assert settings.agent_runtime_version == "legacy"


@pytest.mark.parametrize("value", ["legacy", "langgraph"])
def test_runtime_flag_accepts_supported_values(
    monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    settings = _settings(monkeypatch, value)
    assert settings.agent_runtime_version == value


@pytest.mark.parametrize("value", ["", "auto", "LANGGRAPH", "legacy,langgraph"])
def test_runtime_flag_rejects_ambiguous_values(
    monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    with pytest.raises(ValidationError):
        _settings(monkeypatch, value)


def test_runtime_flag_is_visible_in_safe_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(monkeypatch, "langgraph")
    assert settings.safe_dump()["agent_runtime_version"] == "langgraph"


def test_langgraph_public_rollout_defaults_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("XUANHU_LANGGRAPH_PUBLIC_ENABLED", raising=False)
    settings = _settings(monkeypatch, None)
    assert settings.langgraph_public_enabled is False


def test_langgraph_public_rollout_uses_explicit_xuanhu_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("XUANHU_LANGGRAPH_PUBLIC_ENABLED", "true")
    settings = _settings(monkeypatch, None)
    assert settings.langgraph_public_enabled is True


def test_l9_rollout_authorities_default_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(monkeypatch, None)
    assert settings.agent_runtime_rollout_phase == "legacy"
    assert settings.langgraph_product_ready is False


@pytest.mark.parametrize(
    "phase",
    [
        "legacy",
        "development",
        "automated_test",
        "internal",
        "canary",
        "full",
        "rollback",
    ],
)
def test_l9_rollout_phase_accepts_only_named_stages(
    monkeypatch: pytest.MonkeyPatch,
    phase: str,
) -> None:
    settings = _settings(monkeypatch, None)
    monkeypatch.setenv("AGENT_RUNTIME_ROLLOUT_PHASE", phase)
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    assert settings.agent_runtime_rollout_phase == phase


def test_l9_rollout_phase_rejects_ambiguous_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _settings(monkeypatch, None)
    monkeypatch.setenv("AGENT_RUNTIME_ROLLOUT_PHASE", "all-users")
    with pytest.raises(ValidationError):
        Settings(_env_file=None)  # type: ignore[call-arg]
