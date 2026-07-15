from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from httpx import ASGITransport, AsyncClient

from app.core.config import get_settings
from app.main import app
from app.services.health import HealthService
from app.services.outbox_metrics import PROMETHEUS_CONTENT_TYPE, render_outbox_prometheus

RULES_PATH = Path(__file__).parents[1] / "deploy" / "prometheus" / "rules" / "xuanhu-outbox-alerts.yml"


def _samples(document: str) -> dict[str, float]:
    samples: dict[str, float] = {}
    for line in document.splitlines():
        if not line or line.startswith("#"):
            continue
        name, raw_value = line.split()
        assert "{" not in name, "Outbox metrics must not use dynamic labels"
        samples[name] = float(raw_value)
    return samples


def test_renderer_exposes_aggregate_gauges_and_the_readiness_threshold_contract() -> None:
    document = render_outbox_prometheus(
        {
            "status": "degraded",
            "backlog_count": 5,
            "pending_count": 4,
            "leased_count": 1,
            "dead_letter_count": 2,
            "oldest_unpublished_age_seconds": 301.25,
            "timestamp": "not-exported",
        },
        publisher_enabled=True,
        ready_max_oldest_age_seconds=300,
        ready_max_dead_letters=0,
    )

    assert document.endswith("\n")
    samples = _samples(document)
    assert samples == {
        "xuanhu_outbox_publisher_enabled": 1,
        "xuanhu_outbox_health_available": 1,
        "xuanhu_outbox_backlog_events": 5,
        "xuanhu_outbox_pending_events": 4,
        "xuanhu_outbox_leased_events": 1,
        "xuanhu_outbox_dead_letter_events": 2,
        "xuanhu_outbox_oldest_unpublished_age_seconds": 301.25,
        "xuanhu_outbox_ready_max_oldest_age_seconds": 300,
        "xuanhu_outbox_ready_max_dead_letter_events": 0,
    }
    assert "not-exported" not in document


@pytest.mark.parametrize(
    ("health", "publisher_enabled", "expected_available", "expected_publisher"),
    [
        (
            {
                "status": "unavailable",
                "backlog_count": 0,
                "pending_count": 0,
                "leased_count": 0,
                "dead_letter_count": 0,
                "oldest_unpublished_age_seconds": 0.0,
            },
            True,
            0,
            1,
        ),
        (
            {
                "status": "disabled",
                "backlog_count": 0,
                "pending_count": 0,
                "leased_count": 0,
                "dead_letter_count": 0,
                "oldest_unpublished_age_seconds": 0.0,
            },
            False,
            1,
            0,
        ),
    ],
)
def test_renderer_distinguishes_health_unavailable_from_publisher_disabled(
    health: dict[str, object],
    publisher_enabled: bool,
    expected_available: int,
    expected_publisher: int,
) -> None:
    document = render_outbox_prometheus(
        health,
        publisher_enabled=publisher_enabled,
        ready_max_oldest_age_seconds=300,
        ready_max_dead_letters=0,
    )

    samples = _samples(document)
    assert samples["xuanhu_outbox_health_available"] == expected_available
    assert samples["xuanhu_outbox_publisher_enabled"] == expected_publisher


def test_renderer_fails_closed_on_schema_drift_and_never_copies_arbitrary_source_text() -> None:
    secret_canaries = {
        "patient_name": "patient-canary",
        "payload": "clinical-payload-canary",
        "exception": "database-password-canary",
        "api_key": "secret-key-canary",
    }
    document = render_outbox_prometheus(
        {
            "status": "ok",
            "backlog_count": 99,
            "pending_count": 1,
            "leased_count": 1,
            "dead_letter_count": 3,
            "oldest_unpublished_age_seconds": 42,
            **secret_canaries,
        },
        publisher_enabled=True,
        ready_max_oldest_age_seconds=300,
        ready_max_dead_letters=0,
    )

    samples = _samples(document)
    assert samples["xuanhu_outbox_health_available"] == 0
    assert samples["xuanhu_outbox_backlog_events"] == 0
    assert samples["xuanhu_outbox_dead_letter_events"] == 0
    for canary in secret_canaries.values():
        assert canary not in document


@pytest.mark.parametrize(
    ("max_age", "max_dead_letters"),
    [(-1.0, 0), (float("inf"), 0), (300.0, -1)],
)
def test_renderer_rejects_invalid_readiness_thresholds(
    max_age: float,
    max_dead_letters: int,
) -> None:
    with pytest.raises(ValueError, match="finite and non-negative"):
        render_outbox_prometheus(
            {
                "status": "ok",
                "backlog_count": 0,
                "pending_count": 0,
                "leased_count": 0,
                "dead_letter_count": 0,
                "oldest_unpublished_age_seconds": 0.0,
            },
            publisher_enabled=True,
            ready_max_oldest_age_seconds=max_age,
            ready_max_dead_letters=max_dead_letters,
        )


@pytest.mark.asyncio
async def test_metrics_endpoint_has_exact_prometheus_content_type_and_no_phi(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OUTBOX_PUBLISHER_ENABLED", "true")
    monkeypatch.setenv("OUTBOX_READY_MAX_OLDEST_AGE_SECONDS", "123.5")
    monkeypatch.setenv("OUTBOX_READY_MAX_DEAD_LETTERS", "2")
    get_settings.cache_clear()

    async def fake_check(_self: HealthService) -> dict[str, object]:
        return {
            "status": "ok",
            "backlog_count": 2,
            "pending_count": 1,
            "leased_count": 1,
            "dead_letter_count": 0,
            "oldest_unpublished_age_seconds": 3.5,
            "patient": "patient-canary",
            "raw_payload": "clinical-payload-canary",
            "api_key": "secret-key-canary",
        }

    monkeypatch.setattr(HealthService, "outbox_check", fake_check)
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.get("/api/v1/metrics/outbox")
    finally:
        get_settings.cache_clear()

    assert response.status_code == 200
    assert response.headers["content-type"] == PROMETHEUS_CONTENT_TYPE
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.text.startswith("# HELP xuanhu_outbox_publisher_enabled ")
    samples = _samples(response.text)
    assert samples["xuanhu_outbox_ready_max_oldest_age_seconds"] == 123.5
    assert samples["xuanhu_outbox_ready_max_dead_letter_events"] == 2
    for forbidden in ("patient-canary", "clinical-payload-canary", "secret-key-canary"):
        assert forbidden not in response.text


def test_prometheus_alert_rules_cover_thresholds_and_operational_failure_modes() -> None:
    document = yaml.safe_load(RULES_PATH.read_text(encoding="utf-8"))
    assert isinstance(document, dict)
    groups = document["groups"]
    assert isinstance(groups, list) and len(groups) == 1
    rules = groups[0]["rules"]
    by_name = {rule["alert"]: rule for rule in rules}

    expected = {
        "XuanhuOutboxBacklogAgeHigh",
        "XuanhuOutboxDeadLettersPresent",
        "XuanhuOutboxHealthUnavailable",
        "XuanhuOutboxPublisherDisabled",
        "XuanhuOutboxMetricsMissing",
    }
    assert set(by_name) == expected

    age_expr = by_name["XuanhuOutboxBacklogAgeHigh"]["expr"]
    assert "xuanhu_outbox_oldest_unpublished_age_seconds" in age_expr
    assert "xuanhu_outbox_ready_max_oldest_age_seconds" in age_expr

    dlq_expr = by_name["XuanhuOutboxDeadLettersPresent"]["expr"]
    assert "xuanhu_outbox_dead_letter_events" in dlq_expr
    assert "xuanhu_outbox_ready_max_dead_letter_events" in dlq_expr

    assert by_name["XuanhuOutboxHealthUnavailable"]["expr"] == "xuanhu_outbox_health_available == 0"
    assert by_name["XuanhuOutboxPublisherDisabled"]["expr"] == "xuanhu_outbox_publisher_enabled == 0"
    missing_expr = by_name["XuanhuOutboxMetricsMissing"]["expr"]
    assert 'up{job="xuanhu-api"}' in missing_expr
    assert "unless on(job, instance) xuanhu_outbox_health_available" in missing_expr

    rendered = str(document).lower()
    for forbidden in ("patient", "session_id", "event_id", "payload", "api_key", "password", "secret"):
        assert forbidden not in rendered
