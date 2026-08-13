"""登录限流客户端 IP 提取（阶段 3 反向代理部署）单元测试。

覆盖 ``_client_ip`` 的 X-Forwarded-For 信任与回退两条路径，防止伪造头
绕过登录限流或反向代理下所有医师共享同一 IP 限流桶。
"""

from __future__ import annotations

import pytest

from app.api.auth import _client_ip
from app.core.config import get_settings


class _FakeClient:
    def __init__(self, host: str) -> None:
        self.host = host


class _FakeRequest:
    def __init__(self, client_host: str, headers: dict[str, str] | None = None) -> None:
        self.client = _FakeClient(client_host)
        self.headers = headers or {}


def test_client_ip_honors_x_forwarded_for_leftmost() -> None:
    """信任代理头时取 X-Forwarded-For 最左侧（真实客户端），忽略代理 IP。"""
    req = _FakeRequest("10.0.0.1", {"x-forwarded-for": "203.0.113.7, 10.0.0.1"})
    assert _client_ip(req) == "203.0.113.7"


def test_client_ip_falls_back_to_direct_when_no_xff() -> None:
    """无 X-Forwarded-For 时回退直连地址（本地/测试环境）。"""
    req = _FakeRequest("127.0.0.1")
    assert _client_ip(req) == "127.0.0.1"


def test_client_ip_ignores_xff_when_proxy_trust_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    """TRUST_PROXY_HEADERS=false 时不信任 XFF，防止伪造头绕过限流。"""
    monkeypatch.setenv("TRUST_PROXY_HEADERS", "false")
    get_settings.cache_clear()
    try:
        req = _FakeRequest("10.0.0.1", {"x-forwarded-for": "203.0.113.7"})
        assert _client_ip(req) == "10.0.0.1"
    finally:
        monkeypatch.delenv("TRUST_PROXY_HEADERS", raising=False)
        get_settings.cache_clear()


def test_client_ip_blank_xff_falls_back() -> None:
    """X-Forwarded-For 为空值时回退直连地址。"""
    req = _FakeRequest("10.0.0.1", {"x-forwarded-for": "  "})
    assert _client_ip(req) == "10.0.0.1"
