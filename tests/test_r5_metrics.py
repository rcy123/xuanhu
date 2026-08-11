"""R5: PHI-safe operational metrics and model-drift detection.

Covers the bounded gateway request-outcome counters, the structured-output
fallback counters, the safety pass/block counter, PHI-safe gateway log
sanitization, and the bounded-label fail-closed guarantee.

All metric assertions are *deltas* captured around a single call, never global
absolute values, because the ``prometheus_client`` registry is a
process-global singleton shared across the test session.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Generator
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx
from httpx import Response
from prometheus_client import REGISTRY
from pydantic import BaseModel, Field

from app.core.config import Settings, get_settings
from app.core.exceptions import (
    ChatOutputTruncatedError,
    ChatStructuredParseError,
    EmbeddingDimensionMismatchError,
    ModelGatewayTimeoutError,
    ModelGatewayUnavailableError,
)
from app.core.gateway import ModelGatewayClient
from app.core.metrics import observe_gateway_request, render_perf_metrics
from app.safety.engine import SafetyRuleEngine
from app.schemas.agent import FormulaResult, HerbDose
from app.schemas.session import PatientInfo

REQ = "xuanhu_gateway_requests_total"
FALLBACK = "xuanhu_gateway_structured_fallback_total"
SAFETY = "xuanhu_safety_checks_total"

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
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_settings(monkeypatch: pytest.MonkeyPatch) -> Generator[Settings, None, None]:
    """测试用 Settings 实例（与 test_gateway.py 一致的口径）。"""
    monkeypatch.setenv("DB_URL", "postgresql://xuanhu:xuanhu_dev@localhost:5432/xuanhu")
    monkeypatch.setenv("REDIS_URL", "redis://:xuanhu_dev@localhost:6379/0")
    monkeypatch.setenv("MODEL_GATEWAY_BASE_URL", "http://mock-gateway:8080/v1")
    monkeypatch.setenv("MODEL_GATEWAY_API_KEY", "sk-test-key-12345")
    monkeypatch.setenv("MODEL_GATEWAY_TIMEOUT_SECONDS", "30")
    monkeypatch.setenv("MODEL_GATEWAY_MAX_RETRIES", "2")
    monkeypatch.setenv("CHAT_MODEL", "test-chat")
    monkeypatch.setenv("EMBEDDING_MODEL", "test-embed")
    monkeypatch.setenv("EMBEDDING_DIM", "768")
    try:
        get_settings.cache_clear()
        yield get_settings()
    finally:
        get_settings.cache_clear()


class SampleOutput(BaseModel):
    """测试用结构化输出 Schema。"""

    name: str
    value: int = Field(ge=0, le=100)


def _schema_named(secret: str) -> type[BaseModel]:
    """Return a BaseModel whose class ``__name__`` carries a dynamic secret."""
    return type(secret, (BaseModel,), {"__annotations__": {"name": str}})


# ---------------------------------------------------------------------------
# Gateway request-outcome counters
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_gateway_chat_success_records_success(mock_settings: Settings) -> None:
    client = ModelGatewayClient(mock_settings)
    counters = _Counters()
    counters.snapshot(REQ, {"operation": "chat", "outcome": "success"})

    with respx.mock:
        respx.post("http://mock-gateway:8080/v1/chat/completions").mock(
            return_value=Response(
                200,
                json={"choices": [{"message": {"content": "ok"}}]},
            )
        )
        result = await client.chat(
            messages=[{"role": "user", "content": "Hello"}],
            trace_id="r5-chat-success",
        )

    assert result == "ok"
    assert counters.delta(REQ, {"operation": "chat", "outcome": "success"}) == 1.0


@pytest.mark.asyncio
async def test_gateway_chat_error_records_error(mock_settings: Settings) -> None:
    """Persistent 5xx after retries records one gateway ``error`` outcome."""
    client = ModelGatewayClient(mock_settings)
    counters = _Counters()
    counters.snapshot(REQ, {"operation": "chat", "outcome": "error"})
    counters.snapshot(REQ, {"operation": "chat", "outcome": "success"})

    with respx.mock:
        respx.post("http://mock-gateway:8080/v1/chat/completions").mock(
            return_value=Response(500, json={"error": "boom"})
        )
        with pytest.raises(ModelGatewayUnavailableError):
            await client.chat(
                messages=[{"role": "user", "content": "Hello"}],
                trace_id="r5-chat-error",
            )

    assert counters.delta(REQ, {"operation": "chat", "outcome": "error"}) == 1.0
    assert counters.delta(REQ, {"operation": "chat", "outcome": "success"}) == 0.0


@pytest.mark.asyncio
async def test_gateway_chat_timeout_records_error(mock_settings: Settings) -> None:
    """A real httpx timeout surfaces as a gateway ``error`` outcome."""
    client = ModelGatewayClient(mock_settings)
    counters = _Counters()
    counters.snapshot(REQ, {"operation": "chat", "outcome": "error"})

    with respx.mock:
        respx.post("http://mock-gateway:8080/v1/chat/completions").mock(
            side_effect=httpx.TimeoutException("request timed out")
        )
        with pytest.raises(ModelGatewayTimeoutError):
            await client.chat(
                messages=[{"role": "user", "content": "Hello"}],
                trace_id="r5-chat-timeout",
            )

    assert counters.delta(REQ, {"operation": "chat", "outcome": "error"}) == 1.0


@pytest.mark.asyncio
async def test_gateway_structured_parse_exhaustion_records_parse_failed(
    mock_settings: Settings,
) -> None:
    """Persistent malformed output records a terminal ``parse_failed`` outcome."""
    client = ModelGatewayClient(mock_settings)
    counters = _Counters()
    counters.snapshot(REQ, {"operation": "chat_structured", "outcome": "parse_failed"})
    counters.snapshot(REQ, {"operation": "chat_structured", "outcome": "success"})

    with respx.mock:
        respx.post("http://mock-gateway:8080/v1/chat/completions").mock(
            return_value=Response(
                200,
                json={"choices": [{"message": {"content": "not valid json"}}]},
            )
        )
        with pytest.raises(ChatStructuredParseError):
            await client.chat_structured(
                messages=[{"role": "user", "content": "Generate"}],
                output_schema=SampleOutput,
                trace_id="r5-parse-exhaust",
            )

    assert counters.delta(REQ, {"operation": "chat_structured", "outcome": "parse_failed"}) == 1.0
    assert counters.delta(REQ, {"operation": "chat_structured", "outcome": "success"}) == 0.0


@pytest.mark.asyncio
async def test_gateway_structured_truncation_records_truncated(mock_settings: Settings) -> None:
    """finish_reason=length is attributed to ``truncated``, not parse_failed."""
    client = ModelGatewayClient(mock_settings)
    counters = _Counters()
    counters.snapshot(REQ, {"operation": "chat_structured", "outcome": "truncated"})
    counters.snapshot(REQ, {"operation": "chat_structured", "outcome": "parse_failed"})

    with respx.mock:
        respx.post("http://mock-gateway:8080/v1/chat/completions").mock(
            return_value=Response(
                200,
                json={
                    "choices": [
                        {
                            "message": {"content": "not valid json"},
                            "finish_reason": "length",
                        }
                    ]
                },
            )
        )
        with pytest.raises(ChatOutputTruncatedError):
            await client.chat_structured(
                messages=[{"role": "user", "content": "Generate"}],
                output_schema=SampleOutput,
                trace_id="r5-truncation",
            )

    assert counters.delta(REQ, {"operation": "chat_structured", "outcome": "truncated"}) == 1.0
    assert counters.delta(REQ, {"operation": "chat_structured", "outcome": "parse_failed"}) == 0.0


@pytest.mark.asyncio
async def test_gateway_embed_success_records_success(mock_settings: Settings) -> None:
    client = ModelGatewayClient(mock_settings)
    counters = _Counters()
    counters.snapshot(REQ, {"operation": "embed", "outcome": "success"})

    fake_embedding = [0.1] * 768
    with respx.mock:
        respx.post("http://mock-gateway:8080/v1/embeddings").mock(
            return_value=Response(
                200,
                json={"data": [{"embedding": fake_embedding}]},
            )
        )
        result = await client.embed(texts=["hello"], trace_id="r5-embed")

    assert len(result) == 1
    assert counters.delta(REQ, {"operation": "embed", "outcome": "success"}) == 1.0


@pytest.mark.asyncio
async def test_gateway_embed_dimension_mismatch_records_error(mock_settings: Settings) -> None:
    """A dimension mismatch is a bounded ``error`` outcome, not a new label."""
    client = ModelGatewayClient(mock_settings)
    counters = _Counters()
    counters.snapshot(REQ, {"operation": "embed", "outcome": "error"})

    with respx.mock:
        respx.post("http://mock-gateway:8080/v1/embeddings").mock(
            return_value=Response(
                200,
                json={"data": [{"embedding": [0.1] * 512}]},
            )
        )
        with pytest.raises(EmbeddingDimensionMismatchError):
            await client.embed(texts=["hello"], trace_id="r5-embed-dim")

    assert counters.delta(REQ, {"operation": "embed", "outcome": "error"}) == 1.0


# ---------------------------------------------------------------------------
# Structured fallback counters
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_structured_fallback_success_records_attempted_and_success(
    mock_settings: Settings,
) -> None:
    """Malformed tool-call arguments fall back to JSON mode and succeed."""
    client = ModelGatewayClient(mock_settings)
    counters = _Counters()
    counters.snapshot(FALLBACK, {"outcome": "attempted"})
    counters.snapshot(FALLBACK, {"outcome": "success"})
    counters.snapshot(FALLBACK, {"outcome": "failure"})
    counters.snapshot(REQ, {"operation": "chat_structured", "outcome": "success"})

    call_payloads: list[dict[str, Any]] = []

    def side_effect(request: httpx.Request) -> Response:
        call_payloads.append(json.loads(request.content.decode()))
        if len(call_payloads) == 1:
            return Response(
                200,
                json={
                    "choices": [
                        {"message": {"tool_calls": [{"function": {"arguments": "not valid json"}}]}}
                    ]
                },
            )
        return Response(
            200,
            json={
                "choices": [{"message": {"content": json.dumps({"name": "fallback", "value": 64})}}]
            },
        )

    with respx.mock:
        respx.post("http://mock-gateway:8080/v1/chat/completions").mock(
            side_effect=side_effect
        )
        result = await client.chat_structured(
            messages=[{"role": "user", "content": "Generate JSON"}],
            output_schema=SampleOutput,
            trace_id="r5-fallback-success",
        )

    assert result.name == "fallback"
    assert counters.delta(FALLBACK, {"outcome": "attempted"}) == 1.0
    assert counters.delta(FALLBACK, {"outcome": "success"}) == 1.0
    assert counters.delta(FALLBACK, {"outcome": "failure"}) == 0.0
    assert counters.delta(REQ, {"operation": "chat_structured", "outcome": "success"}) == 1.0


@pytest.mark.asyncio
async def test_structured_fallback_failure_records_failure_until_exhaustion(
    mock_settings: Settings,
) -> None:
    """Every attempt returns non-empty malformed JSON so each falls back and fails."""
    client = ModelGatewayClient(mock_settings)
    counters = _Counters()
    counters.snapshot(FALLBACK, {"outcome": "attempted"})
    counters.snapshot(FALLBACK, {"outcome": "success"})
    counters.snapshot(FALLBACK, {"outcome": "failure"})
    counters.snapshot(REQ, {"operation": "chat_structured", "outcome": "parse_failed"})

    with respx.mock:
        respx.post("http://mock-gateway:8080/v1/chat/completions").mock(
            return_value=Response(
                200,
                json={"choices": [{"message": {"content": "not valid json"}}]},
            )
        )
        with pytest.raises(ChatStructuredParseError):
            await client.chat_structured(
                messages=[{"role": "user", "content": "Generate"}],
                output_schema=SampleOutput,
                trace_id="r5-fallback-failure",
            )

    # max_retries=2 -> 3 attempts; each non-empty failure triggers one fallback
    # that also fails.  One terminal parse_failed is recorded at request level.
    assert counters.delta(FALLBACK, {"outcome": "attempted"}) == 3.0
    assert counters.delta(FALLBACK, {"outcome": "failure"}) == 3.0
    assert counters.delta(FALLBACK, {"outcome": "success"}) == 0.0
    assert counters.delta(REQ, {"operation": "chat_structured", "outcome": "parse_failed"}) == 1.0


@pytest.mark.asyncio
async def test_structured_bounded_path_never_falls_back(mock_settings: Settings) -> None:
    """A bounded caller (max_requests=1) never expands into a JSON fallback."""
    client = ModelGatewayClient(mock_settings)
    counters = _Counters()
    counters.snapshot(FALLBACK, {"outcome": "attempted"})
    counters.snapshot(REQ, {"operation": "chat_structured", "outcome": "parse_failed"})

    with respx.mock:
        route = respx.post("http://mock-gateway:8080/v1/chat/completions").mock(
            return_value=Response(
                200,
                json={"choices": [{"message": {"content": "not valid json"}}]},
            )
        )
        with pytest.raises(ChatStructuredParseError):
            await client.chat_structured(
                messages=[{"role": "user", "content": "Generate"}],
                output_schema=SampleOutput,
                trace_id="r5-bounded-no-fallback",
                max_requests=1,
            )

    assert len(route.calls) == 1
    assert counters.delta(FALLBACK, {"outcome": "attempted"}) == 0.0
    assert counters.delta(REQ, {"operation": "chat_structured", "outcome": "parse_failed"}) == 1.0


# ---------------------------------------------------------------------------
# Bounded-label fail-closed guarantee
# ---------------------------------------------------------------------------


def test_invalid_label_values_fail_closed_to_unknown_bucket() -> None:
    """Arbitrary caller data can never create a new label time series."""
    counters = _Counters()
    counters.snapshot(REQ, {"operation": "unknown", "outcome": "unknown"})

    observe_gateway_request("EVIL_OPERATION_9f3a", "EVIL_OUTCOME_7b2c")

    rendered = render_perf_metrics()
    assert counters.delta(REQ, {"operation": "unknown", "outcome": "unknown"}) == 1.0
    assert "EVIL_OPERATION_9f3a" not in rendered
    assert "EVIL_OUTCOME_7b2c" not in rendered


def test_render_contains_r5_counters(mock_settings: Settings) -> None:
    """The metrics endpoint renders the bounded R5 counters alongside histograms."""
    rendered = render_perf_metrics()
    assert "# HELP xuanhu_gateway_requests_total" in rendered
    assert "# HELP xuanhu_gateway_structured_fallback_total" in rendered
    assert "# HELP xuanhu_safety_checks_total" in rendered
    assert "# HELP xuanhu_gateway_chat_seconds" in rendered


# ---------------------------------------------------------------------------
# PHI-safe log sanitization
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_gateway_logs_never_leak_phi(
    mock_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Malicious trace_id / schema / base URL / route / prompt / error stay out of logs."""
    monkeypatch.setenv("MODEL_GATEWAY_ROUTE_PROFILE", "EVIL-ROUTE-1a2b")
    get_settings.cache_clear()
    client = ModelGatewayClient(get_settings())

    secret_trace = "EVIL-TRACE-9f3a-<<SECRET>>"
    secret_schema = "EVIL_SCHEMA_7b2c"
    secret_prompt = "EVIL-PROMPT-4d5e-<<CLINICAL>>"
    secret_model = "EVIL-MODEL-6f0a-<<MODEL>>"

    with caplog.at_level(logging.DEBUG, logger="xuanhu.gateway"), respx.mock:
        # malformed tool_calls -> fallback -> valid JSON, exercising every
        # log branch (request, parse failure, fallback request, completion).
        call_payloads: list[dict[str, Any]] = []

        def side_effect(request: httpx.Request) -> Response:
            call_payloads.append(json.loads(request.content.decode()))
            if len(call_payloads) == 1:
                return Response(
                    200,
                    json={
                        "choices": [
                            {
                                "message": {
                                    "tool_calls": [{"function": {"arguments": "not valid json"}}]
                                }
                            }
                        ]
                    },
                )
            return Response(
                200,
                json={
                    "choices": [
                        {"message": {"content": json.dumps({"name": "x", "value": 1})}}
                    ]
                },
            )

        respx.post("http://mock-gateway:8080/v1/chat/completions").mock(
            side_effect=side_effect
        )
        await client.chat_structured(
            messages=[{"role": "user", "content": secret_prompt}],
            output_schema=_schema_named(secret_schema),
            model=secret_model,
            trace_id=secret_trace,
        )

    log_text = caplog.text
    # The prompt still reaches the wire (transport must be untouched)...
    assert any(secret_prompt in json.dumps(p) for p in call_payloads)
    # ...but never the logs, and neither do the dynamic identifiers.
    assert secret_trace not in log_text
    assert secret_schema not in log_text
    assert secret_prompt not in log_text
    assert secret_model not in log_text
    assert "mock-gateway" not in log_text
    assert "EVIL-ROUTE-1a2b" not in log_text
    assert "sk-test-key-12345" not in log_text


@pytest.mark.asyncio
async def test_gateway_error_text_not_logged(
    mock_settings: Settings,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Transport error detail strings never reach gateway logs."""
    client = ModelGatewayClient(mock_settings)
    with caplog.at_level(logging.DEBUG, logger="xuanhu.gateway"), respx.mock:
        respx.post("http://mock-gateway:8080/v1/chat/completions").mock(
            side_effect=httpx.ConnectError("EVIL-CONNECT-DETAIL-zz9")
        )
        with pytest.raises(ModelGatewayUnavailableError):
            await client.chat(
                messages=[{"role": "user", "content": "Hello"}],
                trace_id="r5-error-text",
            )
    assert "EVIL-CONNECT-DETAIL-zz9" not in caplog.text


# ---------------------------------------------------------------------------
# Safety pass / block counters (authoritative decision)
# ---------------------------------------------------------------------------


class _FakeHerb:
    """Minimal Herb ORM stand-in matching the rule engine's attribute contract."""

    def __init__(
        self,
        name: str,
        aliases: list[str] | None = None,
        max_dose: float | None = None,
    ) -> None:
        self.name = name
        self.aliases = aliases or []
        self.max_dose = max_dose
        self.pregnancy_contraindication = "none"
        self.incompatibilities: list[dict[str, str]] | None = None
        self.contraindications: list[Any] | None = None

    def __hash__(self) -> int:
        return hash(self.name)


class _FakeUnit:
    """Minimal DosageUnit ORM stand-in."""

    def __init__(self, unit_name: str, to_grams: float = 1.0) -> None:
        self.unit_name = unit_name
        self.aliases: list[str] = []
        self.to_grams = to_grams
        self.conversion_type = "standard"
        self.enabled = True


class _FakeScalarResult:
    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows

    def all(self) -> list[Any]:
        return self._rows


class _FakeExecuteResult:
    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows

    def scalars(self) -> _FakeScalarResult:
        return _FakeScalarResult(self._rows)


class _DualDb:
    """Return herb rows for the herb query and unit rows for the unit query."""

    def __init__(self, herbs: list[Any], units: list[Any]) -> None:
        self._herbs = herbs
        self._units = units

    async def execute(self, statement: object) -> _FakeExecuteResult:
        if "dosage_units" in str(statement):
            return _FakeExecuteResult(self._units)
        return _FakeExecuteResult(self._herbs)


def _formula(composition: list[HerbDose]) -> FormulaResult:
    return FormulaResult(name="test", composition=composition, rationale="测试")


@pytest.mark.asyncio
async def test_safety_blocked_path_records_blocked() -> None:
    """An unknown herb in an empty knowledge base is a real blocked decision."""
    db = _DualDb([], [])
    engine = SafetyRuleEngine(db)  # type: ignore[arg-type]
    counters = _Counters()
    counters.snapshot(SAFETY, {"outcome": "blocked"})
    counters.snapshot(SAFETY, {"outcome": "passed"})

    result = await engine.evaluate(
        _formula([HerbDose(herb="党参", dose=10, unit="g")]),
        PatientInfo(),
        observe_metric=True,
    )

    assert result.passed is False
    assert counters.delta(SAFETY, {"outcome": "blocked"}) == 1.0
    assert counters.delta(SAFETY, {"outcome": "passed"}) == 0.0


@pytest.mark.asyncio
async def test_safety_passed_path_records_passed() -> None:
    """A known herb with no violations is a real passed decision."""
    db = _DualDb([_FakeHerb("党参", max_dose=30)], [_FakeUnit("g")])
    engine = SafetyRuleEngine(db)  # type: ignore[arg-type]
    counters = _Counters()
    counters.snapshot(SAFETY, {"outcome": "passed"})
    counters.snapshot(SAFETY, {"outcome": "blocked"})

    result = await engine.evaluate(
        _formula([HerbDose(herb="党参", dose=10, unit="g")]),
        PatientInfo(),
        observe_metric=True,
    )

    assert result.passed is True
    assert counters.delta(SAFETY, {"outcome": "passed"}) == 1.0
    assert counters.delta(SAFETY, {"outcome": "blocked"}) == 0.0


@pytest.mark.asyncio
async def test_safety_advisory_precheck_does_not_observe() -> None:
    """evaluate() defaults to no metric observation (opt-in via observe_metric=True)."""
    db = _DualDb([], [])
    engine = SafetyRuleEngine(db)  # type: ignore[arg-type]
    counters = _Counters()
    counters.snapshot(SAFETY, {"outcome": "blocked"})
    counters.snapshot(SAFETY, {"outcome": "passed"})

    # The observe_metric keyword is deliberately omitted to prove the default
    # (advisory) evaluation does not count towards the safety metric.
    result = await engine.evaluate(
        _formula([HerbDose(herb="党参", dose=10, unit="g")]),
        PatientInfo(),
    )

    assert result.passed is False
    assert counters.delta(SAFETY, {"outcome": "blocked"}) == 0.0
    assert counters.delta(SAFETY, {"outcome": "passed"}) == 0.0


# ---------------------------------------------------------------------------
# Alert-rule structural guard (works without docker/promtool in CI unit job)
# ---------------------------------------------------------------------------

_RULES_PATH = Path("deploy/prometheus/rules/xuanhu-r5-alerts.yml")


def _rules_text() -> str:
    return _RULES_PATH.read_text(encoding="utf-8")


def test_r5_alert_rules_define_all_drift_alerts() -> None:
    """Every R5 drift alert required by the contract is defined."""
    text = _rules_text()
    for name in (
        "XuanhuStructuredTerminalFailureRateHigh",
        "XuanhuStructuredFallbackRateHigh",
        "XuanhuStructuredFallbackFailureRateHigh",
        "XuanhuSafetyBlockRateDrift",
    ):
        assert f"- alert: {name}" in text


def test_r5_alert_rules_use_only_bounded_static_labels() -> None:
    """Alert labels are a fixed, finite set with no PHI/dynamic identifiers."""
    text = _rules_text()
    # every rule carries exactly the bounded label triple
    assert text.count("severity:") >= 4
    assert text.count("service: xuanhu") >= 4
    assert text.count("component:") >= 4
    # every rule has severity/runbook annotations and a minimum-volume guard
    assert text.count("runbook:") >= 4
    assert text.count("and sum(increase(") >= 4
    # no dynamic or PHI-derived label names/values anywhere in the rules
    for forbidden in (
        "patient",
        "session",
        "trace_id",
        "trace",
        "model_name",
        "schema",
        "base_url",
        "route_profile",
        "host",
        "agent_name",
    ):
        assert forbidden not in text


def test_r5_alert_rule_tests_cover_positive_and_negative() -> None:
    """Each drift alert has both a firing and a quiet promtool scenario."""
    test_text = Path("deploy/prometheus/tests/xuanhu-r5-alerts.test.yml").read_text(
        encoding="utf-8"
    )
    for name in (
        "XuanhuStructuredTerminalFailureRateHigh",
        "XuanhuStructuredFallbackRateHigh",
        "XuanhuStructuredFallbackFailureRateHigh",
        "XuanhuSafetyBlockRateDrift",
    ):
        assert f"alertname: {name}" in test_text


# ---------------------------------------------------------------------------
# Observation must never alter business behavior (failing metric sinks)
#
# These patch the observe callables *as imported into the module under test*
# (the production call boundary) so the assertion exercises the real gateway
# and safety call paths, not the metrics module in isolation.
# ---------------------------------------------------------------------------


def _raise_metric_sink_down(*args: Any) -> None:
    raise RuntimeError("metric sink down")


@pytest.mark.asyncio
async def test_gateway_success_preserved_when_observe_raises(
    mock_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failing gateway-outcome sink must not mask a successful call."""
    monkeypatch.setattr(
        "app.core.gateway.observe_gateway_request",
        _raise_metric_sink_down,
    )
    client = ModelGatewayClient(mock_settings)

    with respx.mock:
        respx.post("http://mock-gateway:8080/v1/chat/completions").mock(
            return_value=Response(200, json={"choices": [{"message": {"content": "ok"}}]})
        )
        result = await client.chat(
            messages=[{"role": "user", "content": "Hello"}],
            trace_id="r5-obs-fail-success",
        )

    assert result == "ok"


@pytest.mark.asyncio
async def test_gateway_structured_parse_exception_preserved_when_observe_raises(
    mock_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failing observe sink must not replace a ChatStructuredParseError."""
    monkeypatch.setattr(
        "app.core.gateway.observe_gateway_request",
        _raise_metric_sink_down,
    )
    client = ModelGatewayClient(mock_settings)

    with respx.mock:
        respx.post("http://mock-gateway:8080/v1/chat/completions").mock(
            return_value=Response(
                200,
                json={"choices": [{"message": {"content": "not valid json"}}]},
            )
        )
        with pytest.raises(ChatStructuredParseError):
            await client.chat_structured(
                messages=[{"role": "user", "content": "Generate"}],
                output_schema=SampleOutput,
                trace_id="r5-obs-fail-parse",
                max_requests=1,
            )


@pytest.mark.asyncio
async def test_gateway_transport_exception_preserved_when_observe_raises(
    mock_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failing observe sink must not replace a ModelGatewayUnavailableError."""
    monkeypatch.setattr(
        "app.core.gateway.observe_gateway_request",
        _raise_metric_sink_down,
    )
    client = ModelGatewayClient(mock_settings)

    with respx.mock:
        respx.post("http://mock-gateway:8080/v1/chat/completions").mock(
            return_value=Response(500, json={"error": "boom"})
        )
        with pytest.raises(ModelGatewayUnavailableError) as exc_info:
            await client.chat(
                messages=[{"role": "user", "content": "Hello"}],
                trace_id="r5-obs-fail-gateway",
            )

    assert exc_info.value.retryable is True


@pytest.mark.asyncio
async def test_safety_result_preserved_when_observe_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failing safety-outcome sink must not alter the authoritative decision."""
    monkeypatch.setattr(
        "app.safety.engine.observe_safety_outcome",
        _raise_metric_sink_down,
    )
    db = _DualDb([], [])
    engine = SafetyRuleEngine(db)  # type: ignore[arg-type]

    result = await engine.evaluate(
        _formula([HerbDose(herb="党参", dose=10, unit="g")]),
        PatientInfo(),
    )

    assert result.passed is False


# ---------------------------------------------------------------------------
# Ordinary-Exception / invalid-response decoding is still a gateway error
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_gateway_invalid_json_response_records_error(mock_settings: Settings) -> None:
    """A 200 with an undecodable body records one ``error`` outcome."""
    client = ModelGatewayClient(mock_settings)
    counters = _Counters()
    counters.snapshot(REQ, {"operation": "chat", "outcome": "error"})
    counters.snapshot(REQ, {"operation": "chat", "outcome": "success"})

    with respx.mock:
        respx.post("http://mock-gateway:8080/v1/chat/completions").mock(
            return_value=Response(200, content=b"{not valid json")
        )
        with pytest.raises(json.JSONDecodeError):
            await client.chat(
                messages=[{"role": "user", "content": "Hello"}],
                trace_id="r5-invalid-json",
            )

    assert counters.delta(REQ, {"operation": "chat", "outcome": "error"}) == 1.0
    assert counters.delta(REQ, {"operation": "chat", "outcome": "success"}) == 0.0


# ---------------------------------------------------------------------------
# Fallback attempted always ends in exactly one success/failure
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_structured_fallback_transport_error_records_failure_and_reraises(
    mock_settings: Settings,
) -> None:
    """A transport error inside the JSON fallback is a fallback failure."""
    client = ModelGatewayClient(mock_settings)
    counters = _Counters()
    counters.snapshot(FALLBACK, {"outcome": "attempted"})
    counters.snapshot(FALLBACK, {"outcome": "success"})
    counters.snapshot(FALLBACK, {"outcome": "failure"})
    counters.snapshot(REQ, {"operation": "chat_structured", "outcome": "error"})

    call_payloads: list[dict[str, Any]] = []

    def side_effect(request: httpx.Request) -> Response:
        call_payloads.append(json.loads(request.content.decode()))
        if len(call_payloads) == 1:
            return Response(
                200,
                json={
                    "choices": [
                        {"message": {"tool_calls": [{"function": {"arguments": "not valid json"}}]}}
                    ]
                },
            )
        raise httpx.ConnectError("fallback transport down")

    with respx.mock:
        respx.post("http://mock-gateway:8080/v1/chat/completions").mock(
            side_effect=side_effect
        )
        with pytest.raises(ModelGatewayUnavailableError):
            await client.chat_structured(
                messages=[{"role": "user", "content": "Generate"}],
                output_schema=SampleOutput,
                trace_id="r5-fallback-transport",
            )

    # Exactly one attempted event, resolved to exactly one failure (no success).
    assert counters.delta(FALLBACK, {"outcome": "attempted"}) == 1.0
    assert counters.delta(FALLBACK, {"outcome": "failure"}) == 1.0
    assert counters.delta(FALLBACK, {"outcome": "success"}) == 0.0
    assert counters.delta(REQ, {"operation": "chat_structured", "outcome": "error"}) == 1.0
