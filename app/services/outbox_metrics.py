"""Privacy-safe Prometheus exposition for the durable Outbox.

The exporter deliberately exposes only fixed-name aggregate gauges.  It never
copies labels, identifiers, payloads, timestamps, exception text, or arbitrary
keys from the health response into the Prometheus document.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass

PROMETHEUS_CONTENT_TYPE = "text/plain; version=0.0.4; charset=utf-8"

_HEALTH_STATUSES = frozenset({"ok", "degraded", "unavailable", "disabled"})


@dataclass(frozen=True, slots=True)
class OutboxPrometheusSnapshot:
    """Validated, aggregate-only state accepted by the metrics renderer."""

    publisher_enabled: int
    health_available: int
    backlog_events: int
    pending_events: int
    leased_events: int
    dead_letter_events: int
    oldest_unpublished_age_seconds: float


def _nonnegative_int(value: object) -> int | None:
    if type(value) is not int or value < 0:
        return None
    return value


def _nonnegative_float(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    result = float(value)
    if not math.isfinite(result) or result < 0:
        return None
    return result


def _snapshot(
    health: Mapping[str, object],
    *,
    publisher_enabled: bool,
) -> OutboxPrometheusSnapshot:
    """Validate the allowlisted aggregate schema and fail closed on drift."""

    status = health.get("status")
    backlog = _nonnegative_int(health.get("backlog_count"))
    pending = _nonnegative_int(health.get("pending_count"))
    leased = _nonnegative_int(health.get("leased_count"))
    dead_letters = _nonnegative_int(health.get("dead_letter_count"))
    oldest_age = _nonnegative_float(health.get("oldest_unpublished_age_seconds"))

    schema_valid = (
        isinstance(status, str)
        and status in _HEALTH_STATUSES
        and backlog is not None
        and pending is not None
        and leased is not None
        and dead_letters is not None
        and oldest_age is not None
        and backlog == pending + leased
        and (status == "disabled") is (not publisher_enabled)
        and (status != "disabled" or (backlog == 0 and dead_letters == 0 and oldest_age == 0))
    )

    if not schema_valid:
        return OutboxPrometheusSnapshot(
            publisher_enabled=int(publisher_enabled),
            health_available=0,
            backlog_events=0,
            pending_events=0,
            leased_events=0,
            dead_letter_events=0,
            oldest_unpublished_age_seconds=0.0,
        )

    assert isinstance(status, str)
    assert backlog is not None
    assert pending is not None
    assert leased is not None
    assert dead_letters is not None
    assert oldest_age is not None
    return OutboxPrometheusSnapshot(
        publisher_enabled=int(publisher_enabled),
        health_available=int(status != "unavailable"),
        backlog_events=backlog,
        pending_events=pending,
        leased_events=leased,
        dead_letter_events=dead_letters,
        oldest_unpublished_age_seconds=oldest_age,
    )


def _format_number(value: int | float) -> str:
    if isinstance(value, int):
        return str(value)
    return format(value, ".15g")


def _gauge(name: str, help_text: str, value: int | float) -> tuple[str, str, str]:
    return (
        f"# HELP {name} {help_text}",
        f"# TYPE {name} gauge",
        f"{name} {_format_number(value)}",
    )


def render_outbox_prometheus(
    health: Mapping[str, object],
    *,
    publisher_enabled: bool,
    ready_max_oldest_age_seconds: float,
    ready_max_dead_letters: int,
) -> str:
    """Render Prometheus text format without dynamic labels or source text.

    The two readiness thresholds are exported as gauges so alerting rules use
    exactly the same operator-configured contract as ``/health/outbox``.
    """

    max_age = _nonnegative_float(ready_max_oldest_age_seconds)
    max_dead_letters = _nonnegative_int(ready_max_dead_letters)
    if max_age is None or max_dead_letters is None:
        raise ValueError("Outbox readiness thresholds must be finite and non-negative")

    snapshot = _snapshot(health, publisher_enabled=publisher_enabled)
    lines: list[str] = []
    gauges = (
        (
            "xuanhu_outbox_publisher_enabled",
            "Whether the durable Outbox publisher is enabled by configuration (1 enabled, 0 disabled).",
            snapshot.publisher_enabled,
        ),
        (
            "xuanhu_outbox_health_available",
            "Whether the Outbox health query returned a valid available result (1 available, 0 unavailable).",
            snapshot.health_available,
        ),
        (
            "xuanhu_outbox_backlog_events",
            "Current number of unpublished Outbox events, including pending and leased events.",
            snapshot.backlog_events,
        ),
        (
            "xuanhu_outbox_pending_events",
            "Current number of pending Outbox events.",
            snapshot.pending_events,
        ),
        (
            "xuanhu_outbox_leased_events",
            "Current number of leased Outbox events.",
            snapshot.leased_events,
        ),
        (
            "xuanhu_outbox_dead_letter_events",
            "Current number of durable Outbox dead-letter events.",
            snapshot.dead_letter_events,
        ),
        (
            "xuanhu_outbox_oldest_unpublished_age_seconds",
            "Age in seconds of the oldest unpublished Outbox event.",
            snapshot.oldest_unpublished_age_seconds,
        ),
        (
            "xuanhu_outbox_ready_max_oldest_age_seconds",
            "Configured readiness and alert threshold for oldest unpublished event age in seconds.",
            max_age,
        ),
        (
            "xuanhu_outbox_ready_max_dead_letter_events",
            "Configured readiness and alert threshold for durable dead-letter events.",
            max_dead_letters,
        ),
    )
    for name, help_text, value in gauges:
        lines.extend(_gauge(name, help_text, value))
    return "\n".join(lines) + "\n"
