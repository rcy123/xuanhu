from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from app.agent_runtime.repository import OutboxHealth, PostgresDomainRepository
from app.core.config import get_settings
from app.main import app
from app.services.health import HealthService


@pytest.mark.asyncio
async def test_outbox_health_reports_explicitly_disabled_without_touching_infrastructure() -> None:
    result = await HealthService().outbox_check()

    assert result == {
        "status": "disabled",
        "backlog_count": 0,
        "pending_count": 0,
        "leased_count": 0,
        "dead_letter_count": 0,
        "oldest_unpublished_age_seconds": 0.0,
        "timestamp": result["timestamp"],
    }


@pytest.mark.asyncio
async def test_enabled_outbox_health_degrades_on_dlq_without_exposing_payloads(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OUTBOX_PUBLISHER_ENABLED", "true")
    get_settings.cache_clear()

    async def fake_health(_self: PostgresDomainRepository) -> OutboxHealth:
        return OutboxHealth(
            backlog_count=5,
            pending_count=4,
            leased_count=1,
            dead_letter_count=1,
            oldest_unpublished_age_seconds=12.5,
        )

    monkeypatch.setattr(PostgresDomainRepository, "get_outbox_health", fake_health)
    try:
        result = await HealthService().outbox_check()
    finally:
        get_settings.cache_clear()

    assert result["status"] == "degraded"
    assert result["dead_letter_count"] == 1
    assert set(result) == {
        "status",
        "backlog_count",
        "pending_count",
        "leased_count",
        "dead_letter_count",
        "oldest_unpublished_age_seconds",
        "timestamp",
    }


@pytest.mark.asyncio
async def test_outbox_health_endpoint_is_flat_and_privacy_safe(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_check(_self: HealthService) -> dict[str, object]:
        return {
            "status": "ok",
            "backlog_count": 2,
            "pending_count": 2,
            "leased_count": 0,
            "dead_letter_count": 0,
            "oldest_unpublished_age_seconds": 3.0,
            "timestamp": "2026-07-14T00:00:00+00:00",
        }

    monkeypatch.setattr(HealthService, "outbox_check", fake_check)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/api/v1/health/outbox")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert "data" not in body
    rendered = str(body).lower()
    for forbidden in ("api_key", "password", "secret", "raw_prompt", "patient"):
        assert forbidden not in rendered
