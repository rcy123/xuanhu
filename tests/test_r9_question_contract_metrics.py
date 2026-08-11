"""R9: PHI-safe question-contract operational metrics.

Covers the bounded question-contract, coverage-evaluation, and follow-up
outcome counters, the label-free aspect-count histogram, the finite-label /
unknown fail-closed guarantee, and the "observation must never alter business
behavior" contract.

All metric assertions are *deltas* captured around a single call, never global
absolute values, because the ``prometheus_client`` registry is a
process-global singleton shared across the test session.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import pytest
from prometheus_client import REGISTRY

from app.core import metrics as metrics_module
from app.core.metrics import (
    observe_question_contract,
    observe_question_coverage,
    observe_question_followup,
    render_perf_metrics,
)

CONTRACT = "xuanhu_question_contracts_total"
COVERAGE = "xuanhu_question_coverage_evaluations_total"
FOLLOWUP = "xuanhu_question_contract_followups_total"
ASPECTS = "xuanhu_question_contract_aspects"

#: Label tokens that must never appear anywhere in the R9 metric surface.
_PHI_TOKENS = ("session", "dimension", "patient", "text", "trace")

# ---------------------------------------------------------------------------
# Metric helpers — delta assertions against a global registry
# ---------------------------------------------------------------------------


def _get(name: str, labels: dict[str, str]) -> float:
    value = REGISTRY.get_sample_value(name, labels)
    return float(value) if value is not None else 0.0


class _Counters:
    """Snapshot a set of metric label combinations and report per-label deltas."""

    def __init__(self) -> None:
        self._base: dict[tuple[str, tuple[tuple[str, str], ...]], float] = {}

    def snapshot(self, name: str, labels: dict[str, str]) -> None:
        key = (name, tuple(sorted(labels.items())))
        self._base[key] = _get(name, labels)

    def delta(self, name: str, labels: dict[str, str]) -> float:
        key = (name, tuple(sorted(labels.items())))
        return _get(name, labels) - self._base.get(key, 0.0)


# ---------------------------------------------------------------------------
# Labels are finite and carry only the declared outcome dimension
# ---------------------------------------------------------------------------


def test_r9_counters_declare_only_outcome_label() -> None:
    """Each R9 counter exposes exactly one label, ``outcome``, with an allowlist."""
    for counter in (
        metrics_module.question_contracts,
        metrics_module.question_coverage_evaluations,
        metrics_module.question_contract_followups,
    ):
        assert counter._labelnames == ("outcome",)
        allow = counter._allowlists["outcome"]
        assert isinstance(allow, frozenset)
        assert allow  # non-empty, finite by construction
        # Every allowlist value is free of PHI / identifier tokens.
        for value in allow:
            assert not any(token in value for token in _PHI_TOKENS)


def test_r9_aspect_histogram_carries_no_labels() -> None:
    """The aspect-count histogram is label-free and uses integer buckets."""
    assert metrics_module.question_contract_aspects._labelnames == ()
    inner = metrics_module.question_contract_aspects._inner
    assert inner is not None
    # prometheus_client appends an explicit +Inf upper bound; the declared
    # finite buckets must match our integer-oriented profile exactly.
    assert inner._upper_bounds[:-1] == [
        float(b) for b in metrics_module._ASPECT_COUNT_BUCKETS
    ]
    assert inner._upper_bounds[0] == 1.0  # integer-oriented, not the seconds profile


# ---------------------------------------------------------------------------
# Unknown fail-closed guarantee
# ---------------------------------------------------------------------------


def test_r9_invalid_outcomes_fail_closed_to_unknown_bucket() -> None:
    """Arbitrary caller data can never create a new label time series."""
    counters = _Counters()
    counters.snapshot(CONTRACT, {"outcome": "unknown"})
    counters.snapshot(COVERAGE, {"outcome": "unknown"})
    counters.snapshot(FOLLOWUP, {"outcome": "unknown"})

    observe_question_contract("EVIL_CONTRACT_9f3a")
    observe_question_coverage("EVIL_COVERAGE_7b2c")
    observe_question_followup("EVIL_FOLLOWUP_4d5e")

    rendered = render_perf_metrics()
    assert counters.delta(CONTRACT, {"outcome": "unknown"}) == 1.0
    assert counters.delta(COVERAGE, {"outcome": "unknown"}) == 1.0
    assert counters.delta(FOLLOWUP, {"outcome": "unknown"}) == 1.0
    for secret in ("EVIL_CONTRACT_9f3a", "EVIL_COVERAGE_7b2c", "EVIL_FOLLOWUP_4d5e"):
        assert secret not in rendered


# ---------------------------------------------------------------------------
# Known outcomes increment their own bounded series
# ---------------------------------------------------------------------------


def test_r9_known_outcomes_increment_own_series() -> None:
    """Every alert-relevant outcome lands on its declared label value."""
    counters = _Counters()
    counters.snapshot(CONTRACT, {"outcome": "created"})
    counters.snapshot(CONTRACT, {"outcome": "integrity_error"})
    counters.snapshot(COVERAGE, {"outcome": "invalid"})
    counters.snapshot(COVERAGE, {"outcome": "error"})
    counters.snapshot(FOLLOWUP, {"outcome": "cap_reached"})

    observe_question_contract("created")
    observe_question_contract("integrity_error")
    observe_question_coverage("invalid")
    observe_question_coverage("error")
    observe_question_followup("cap_reached")

    assert counters.delta(CONTRACT, {"outcome": "created"}) == 1.0
    assert counters.delta(CONTRACT, {"outcome": "integrity_error"}) == 1.0
    assert counters.delta(COVERAGE, {"outcome": "invalid"}) == 1.0
    assert counters.delta(COVERAGE, {"outcome": "error"}) == 1.0
    assert counters.delta(FOLLOWUP, {"outcome": "cap_reached"}) == 1.0


# ---------------------------------------------------------------------------
# Observation must never alter business behavior
# ---------------------------------------------------------------------------


class _BoomSink:
    """Metric sink whose every interaction raises — models a failed registry."""

    def inc(self, *args: Any, **kwargs: Any) -> None:
        raise RuntimeError("metric sink down")

    def observe(self, *args: Any, **kwargs: Any) -> None:
        raise RuntimeError("metric sink down")


@pytest.mark.parametrize(
    ("call", "sink", "diagnostic"),
    [
        (
            lambda: observe_question_contract("created", aspect_count=3),
            ("question_contracts", "question_contract_aspects"),
            "question contract metric observation failed",
        ),
        (
            lambda: observe_question_coverage("invalid"),
            ("question_coverage_evaluations",),
            "question coverage metric observation failed",
        ),
        (
            lambda: observe_question_followup("cap_reached"),
            ("question_contract_followups",),
            "question follow-up metric observation failed",
        ),
    ],
)
def test_r9_observe_swallows_sink_failures(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    call: Any,
    sink: tuple[str, ...],
    diagnostic: str,
) -> None:
    """A failed metric sink never raises out of the observe boundary."""
    for name in sink:
        monkeypatch.setattr(metrics_module, name, _BoomSink())
    with caplog.at_level(logging.WARNING, logger="xuanhu.metrics"):
        assert call() is None  # observe functions return None, never raise
    assert diagnostic in caplog.text


def test_r9_malformed_aspect_count_degrades_only_the_histogram(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A bad aspect count is a diagnostic, not a business error — and the
    contract outcome counter is still recorded."""
    counters = _Counters()
    counters.snapshot(CONTRACT, {"outcome": "created"})

    with caplog.at_level(logging.WARNING, logger="xuanhu.metrics"):
        assert observe_question_contract("created", aspect_count="not-a-number") is None

    # The contract outcome still landed; only the aspect histogram was dropped.
    assert counters.delta(CONTRACT, {"outcome": "created"}) == 1.0
    assert "question contract aspect count observation failed" in caplog.text


def test_r9_observe_is_noop_when_registry_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With no backing registry the observe functions are silent no-ops."""
    for counter in (
        metrics_module.question_contracts,
        metrics_module.question_coverage_evaluations,
        metrics_module.question_contract_followups,
        metrics_module.question_contract_aspects,
    ):
        # _inner is None exactly when prometheus_client was unavailable at
        # import time; every inc()/observe() short-circuits without recording.
        monkeypatch.setattr(counter, "_inner", None)

    assert observe_question_contract("created", aspect_count=3) is None
    assert observe_question_coverage("invalid") is None
    assert observe_question_followup("asked") is None


# ---------------------------------------------------------------------------
# Rendered surface is PHI-free and complete
# ---------------------------------------------------------------------------


def _family_lines(rendered: str, metric: str) -> list[str]:
    return [ln for ln in rendered.splitlines() if ln.startswith(metric)]


def test_r9_render_contains_counters_and_aspect_histogram() -> None:
    """The metrics endpoint renders every R9 metric family."""
    rendered = render_perf_metrics()
    assert "# HELP xuanhu_question_contracts_total" in rendered
    assert "# HELP xuanhu_question_coverage_evaluations_total" in rendered
    assert "# HELP xuanhu_question_contract_followups_total" in rendered
    assert "# HELP xuanhu_question_contract_aspects" in rendered
    assert "xuanhu_question_contract_aspect_count" not in rendered  # renamed


def test_r9_rendered_metric_lines_carry_no_phi_tokens() -> None:
    """No session/dimension/text label or value anywhere on the R9 families."""
    rendered = render_perf_metrics()
    families = (
        _family_lines(rendered, "xuanhu_question_contracts_total")
        + _family_lines(rendered, "xuanhu_question_coverage_evaluations_total")
        + _family_lines(rendered, "xuanhu_question_contract_followups_total")
        + _family_lines(rendered, "xuanhu_question_contract_aspects")
    )
    assert families  # the families actually exported series
    for line in families:
        for token in _PHI_TOKENS:
            assert token not in line


# ---------------------------------------------------------------------------
# Alert-rule structural guard (works without docker/promtool in CI unit job)
# ---------------------------------------------------------------------------

_RULES_PATH = Path("deploy/prometheus/rules/xuanhu-r9-alerts.yml")
_TESTS_PATH = Path("deploy/prometheus/tests/xuanhu-r9-alerts.test.yml")

_R9_ALERTS = (
    "XuanhuQuestionCoverageFailureRateHigh",
    "XuanhuQuestionCapReachedHigh",
    "XuanhuQuestionContractIntegrityError",
)


def _rules_text() -> str:
    return _RULES_PATH.read_text(encoding="utf-8")


def test_r9_alert_rules_define_all_contract_alerts() -> None:
    """Every R9 alert required by the contract is defined."""
    text = _rules_text()
    for name in _R9_ALERTS:
        assert f"- alert: {name}" in text


def test_r9_alert_rules_use_only_bounded_static_labels() -> None:
    """Alert labels are a fixed, finite set with no PHI/dynamic identifiers."""
    text = _rules_text()
    assert text.count("severity:") >= len(_R9_ALERTS)
    assert text.count("service: xuanhu") >= len(_R9_ALERTS)
    assert text.count("component:") >= len(_R9_ALERTS)
    # every rule has severity/runbook annotations and a minimum-volume guard
    assert text.count("runbook:") >= len(_R9_ALERTS)
    assert text.count("and sum(increase(") >= len(_R9_ALERTS)
    # the rules reference only the bounded R9 counters — no other metric family
    assert "xuanhu_question_coverage_evaluations_total" in text
    assert "xuanhu_question_contract_followups_total" in text
    assert "xuanhu_question_contracts_total" in text
    assert "xuanhu_gateway_" not in text
    assert "xuanhu_safety_" not in text
    # no dynamic or PHI-derived label names/values anywhere in the rules
    for forbidden in (
        "patient",
        "session",
        "dimension",
        "contract_id",
        "message_id",
        "question_id",
        "trace",
        "text",
        "base_url",
        "model_name",
    ):
        assert forbidden not in text


def test_r9_alert_rule_tests_cover_positive_and_negative() -> None:
    """Each R9 alert has both a firing and a quiet promtool scenario."""
    test_text = _TESTS_PATH.read_text(encoding="utf-8")
    for name in _R9_ALERTS:
        assert f"alertname: {name}" in test_text
    # quiet scenarios are expressed as empty expected alerts
    assert "exp_alerts: []" in test_text
