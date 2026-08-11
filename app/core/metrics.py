"""Per-stage Prometheus metrics for the performance baseline and R5 outcomes.

All histograms use the same bucket profile tuned for millisecond-to-second
ranges: 0.01/0.05/0.1/0.25/0.5/1/2.5/5/10/30.  Labels never carry
high-cardinality or PHI values (no session_id, patient_ref, or query text).

R5 adds bounded, low-cardinality outcome counters (gateway request outcomes,
structured-output fallback attempts, and safety pass/block) whose label values
are drawn exclusively from a finite allowlist.  Any unexpected label value is
fail-closed to a fixed ``unknown`` bucket rather than creating a new time
series.

Usage::

    async with measure("rag.vector") as m:
        results = await search(...)
    # m.seconds is now populated, histogram observed automatically.

"""

from __future__ import annotations

import logging
import math
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger("xuanhu.metrics")

# ---------------------------------------------------------------------------
# Bucket profile — broad enough for all stages without instrument-specific
# bucket lists.  Covers ingest (100ms-30s), LLM gateway (1s-30s),
# and lightweight DB reads (10ms-1s).
# ---------------------------------------------------------------------------
_DEFAULT_BUCKETS = (0.01, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0)


# ---------------------------------------------------------------------------
# Typed metric registry (backed by prometheus_client when available)
# ---------------------------------------------------------------------------

#: Sentinel bucket for any label value that is not on a declared allowlist.
#: Mapping an unexpected value here (rather than emitting a fresh time series)
#: is the fail-closed guarantee required by the R5 metric contract.
_UNKNOWN_LABEL = "unknown"


def _bounded(value: str, allowlist: frozenset[str], default: str = _UNKNOWN_LABEL) -> str:
    """Return ``value`` if it is on ``allowlist``, else a fixed ``default``.

    Guarantees that a bounded counter can never create an unbounded set of
    label time series from arbitrary caller data.
    """
    return value if value in allowlist else default


class _Histogram:
    """Simple histogram container — wraps prometheus_client if installed."""

    __slots__ = ("_inner", "_labelnames")

    def __init__(self, name: str, description: str, labelnames: tuple[str, ...] = ()) -> None:
        self._inner: Any = None
        self._labelnames: tuple[str, ...] = tuple(labelnames)
        try:
            import prometheus_client

            if self._labelnames:
                self._inner = prometheus_client.Histogram(
                    name,
                    description,
                    labelnames=self._labelnames,
                    buckets=_DEFAULT_BUCKETS,
                )
            else:
                self._inner = prometheus_client.Histogram(
                    name,
                    description,
                    buckets=_DEFAULT_BUCKETS,
                )
        except ImportError:
            logger.debug("prometheus_client not available — metrics are no-ops")

    def observe(self, value: float, labels: dict[str, str] | None = None) -> None:
        """Observe ``value`` under the declared labels only.

        Non-finite or negative values are ignored (never pollute a histogram).
        Labels are forwarded only for keys declared on the histogram; anything
        else is silently dropped so caller data cannot create new series.
        """
        if self._inner is None:
            return
        try:
            if not math.isfinite(value) or value < 0:
                return
        except (TypeError, ValueError):
            return
        if labels:
            allowed = {key: labels[key] for key in self._labelnames if key in labels}
            if self._labelnames and len(allowed) == len(self._labelnames):
                self._inner.labels(**allowed).observe(value)
                return
            if not self._labelnames:
                self._inner.observe(value)
                return
            # A labelled histogram is missing a declared label — drop the
            # observation rather than fail closed into a bogus time series.
            return
        if not self._labelnames:
            self._inner.observe(value)


class _Counter:
    """Bounded counter — wraps prometheus_client.Counter if installed.

    ``allowlists`` maps each declared label name to the finite set of values it
    may carry.  Any value outside the allowlist is fail-closed to
    ``_UNKNOWN_LABEL`` so caller data can never expand the label space.
    """

    __slots__ = ("_inner", "_labelnames", "_allowlists")

    def __init__(
        self,
        name: str,
        description: str,
        labelnames: tuple[str, ...] = (),
        allowlists: dict[str, frozenset[str]] | None = None,
    ) -> None:
        self._inner: Any = None
        self._labelnames: tuple[str, ...] = tuple(labelnames)
        self._allowlists: dict[str, frozenset[str]] = dict(allowlists or {})
        try:
            import prometheus_client

            if self._labelnames:
                self._inner = prometheus_client.Counter(
                    name,
                    description,
                    labelnames=self._labelnames,
                )
            else:
                self._inner = prometheus_client.Counter(name, description)
        except ImportError:
            logger.debug("prometheus_client not available — metrics are no-ops")

    def inc(self, labels: dict[str, str] | None = None) -> None:
        """Increment by one under bounded, allowlisted labels."""
        if self._inner is None:
            return
        if self._labelnames:
            bounded: dict[str, str] = {}
            for label in self._labelnames:
                value = labels.get(label) if labels else None
                allow = self._allowlists.get(label)
                if allow is None or value is None or value not in allow:
                    bounded[label] = _UNKNOWN_LABEL
                else:
                    bounded[label] = value
            self._inner.labels(**bounded).inc()
        else:
            self._inner.inc()


# ---------------------------------------------------------------------------
# Declare all histograms
# ---------------------------------------------------------------------------

# RAG stage durations
rag_vector_search = _Histogram(
    "xuanhu_rag_vector_search_seconds",
    "Milvus vector search duration (embed + search)",
)
rag_fulltext_search = _Histogram(
    "xuanhu_rag_fulltext_search_seconds",
    "PG fulltext search duration",
)
rag_backfill = _Histogram(
    "xuanhu_rag_backfill_seconds",
    "Content snippet backfill duration (PG round-trip)",
)
rag_embed = _Histogram(
    "xuanhu_rag_embed_seconds",
    "Embedding generation duration (gateway round-trip)",
)

# Gateway durations.  R5: the chat histogram no longer carries dynamic
# ``host``/``route_profile`` labels (PHI/cardinality risk).  It is deliberately
# unlabelled — per-path attribution is derived from the bounded outcome counter
# below, not from per-instance label values.
gateway_chat = _Histogram(
    "xuanhu_gateway_chat_seconds",
    "Gateway chat/chat_structured duration",
)
gateway_embed = _Histogram(
    "xuanhu_gateway_embed_seconds",
    "Gateway embed duration",
)

# Graph node durations (labelled by subgraph and node name)
graph_node = _Histogram(
    "xuanhu_graph_node_seconds",
    "Per-node duration inside a LangGraph subgraph",
    labelnames=("subgraph", "node"),
)

# Reasoning get_state durations (OP1: authority snapshot cache)
reasoning_get_state = _Histogram(
    "xuanhu_reasoning_get_state_seconds",
    "Reasoning authority snapshot get_state DB round-trip duration",
)

# ---------------------------------------------------------------------------
# R5 outcome counters (bounded, low-cardinality labels only)
# ---------------------------------------------------------------------------

#: Gateway call kinds instrumented on the production ModelGateway call paths.
_GATEWAY_OPERATIONS = frozenset({"chat", "chat_structured", "embed"})
#: Terminal outcome of a single top-level gateway call (one per call).
_GATEWAY_OUTCOMES = frozenset({"success", "error", "truncated", "parse_failed"})
#: Structured-output JSON fallback attempt outcomes (parse-level only).
_FALLBACK_OUTCOMES = frozenset({"attempted", "success", "failure"})
#: Authoritative safety decision outcomes.
_SAFETY_OUTCOMES = frozenset({"passed", "blocked"})

# One increment per top-level chat/chat_structured/embed call.  ``success`` is
# a completed call; ``error`` is any gateway transport/response failure
# (unavailable, timeout, malformed response); ``truncated`` and ``parse_failed``
# are structured-output terminal failures kept separate from one another.
gateway_requests = _Counter(
    "xuanhu_gateway_requests_total",
    "Total ModelGateway top-level calls by bounded operation and outcome",
    labelnames=("operation", "outcome"),
    allowlists={"operation": _GATEWAY_OPERATIONS, "outcome": _GATEWAY_OUTCOMES},
)

# One increment per JSON-mode structured fallback attempt (legacy, unbounded
# caller path).  ``success``/``failure`` are parse-level resolutions; a
# transport error mid-fallback surfaces at request level as ``error`` instead.
# The numerator counts *attempts*, so it can exceed the number of top-level
# structured calls when the caller retries and each retry falls back.
gateway_structured_fallback = _Counter(
    "xuanhu_gateway_structured_fallback_total",
    "Structured-output JSON fallback attempts by bounded outcome",
    labelnames=("outcome",),
    allowlists={"outcome": _FALLBACK_OUTCOMES},
)

# One increment per authoritative SafetyRuleEngine decision (after the
# passed/blocked decision exists).  Used to detect safety block-rate drift.
safety_checks = _Counter(
    "xuanhu_safety_checks_total",
    "Authoritative safety rule decisions by bounded passed/blocked outcome",
    labelnames=("outcome",),
    allowlists={"outcome": _SAFETY_OUTCOMES},
)


def observe_gateway_request(operation: str, outcome: str) -> None:
    """Record one bounded gateway request outcome (fail-closed on both labels).

    Observation is best-effort and must never alter business behavior: any
    failure while persisting the metric is swallowed, emitting at most a
    static diagnostic with no dynamic caller input.
    """
    try:
        gateway_requests.inc(
            {
                "operation": _bounded(operation, _GATEWAY_OPERATIONS),
                "outcome": _bounded(outcome, _GATEWAY_OUTCOMES),
            }
        )
    except Exception:  # noqa: BLE001 - observation must never raise into the call path
        logger.warning("gateway request metric observation failed")


def observe_gateway_structured_fallback(outcome: str) -> None:
    """Record one bounded structured-fallback outcome (fail-closed).

    Best-effort: a metric failure is swallowed (static diagnostic only) so it
    never alters gateway behavior.
    """
    try:
        gateway_structured_fallback.inc(
            {"outcome": _bounded(outcome, _FALLBACK_OUTCOMES)}
        )
    except Exception:  # noqa: BLE001 - observation must never raise into the call path
        logger.warning("gateway structured-fallback metric observation failed")


def observe_safety_outcome(passed: bool) -> None:
    """Record one authoritative safety decision as ``passed`` or ``blocked``.

    Best-effort: a metric failure is swallowed (static diagnostic only) so it
    never alters the safety decision.
    """
    try:
        safety_checks.inc({"outcome": "passed" if passed else "blocked"})
    except Exception:  # noqa: BLE001 - observation must never raise into the call path
        logger.warning("safety outcome metric observation failed")

# ---------------------------------------------------------------------------
# Measure context manager
# ---------------------------------------------------------------------------

@dataclass
class MeasureResult:
    """Result of a ``measure()`` block — elapsed seconds are set on exit."""

    seconds: float = 0.0


@asynccontextmanager
async def measure(
    stage: str,
    labels: dict[str, str] | None = None,
) -> AsyncIterator[MeasureResult]:
    """Time a block and observe the matching histogram.

    The ``stage`` key must be one of the declared histogram names (sans
    ``xuanhu_`` prefix and ``_seconds`` suffix), e.g. ``"rag.vector"``
    for ``xuanhu_rag_vector_search_seconds``.

    When ``labels`` contains high-cardinality keys they are silently dropped
    to prevent label explosion — only the declared labelnames are forwarded.
    """
    result = MeasureResult()
    _t0 = time.perf_counter()
    try:
        yield result
    finally:
        elapsed = time.perf_counter() - _t0
        result.seconds = elapsed
        _observe(stage, elapsed, labels=labels)


def _observe(stage: str, value: float, labels: dict[str, str] | None = None) -> None:
    """Route a (stage, value) pair to the matching histogram."""
    _MAP: dict[str, _Histogram] = {
        "rag.vector": rag_vector_search,
        "rag.fulltext": rag_fulltext_search,
        "rag.backfill": rag_backfill,
        "rag.embed": rag_embed,
        "gateway.chat": gateway_chat,
        "gateway.embed": gateway_embed,
        "graph.node": graph_node,
        "reasoning.get_state": reasoning_get_state,
    }
    h = _MAP.get(stage)
    if h is not None:
        h.observe(value, labels=labels)


# ---------------------------------------------------------------------------
# Render all histograms as Prometheus text format
# ---------------------------------------------------------------------------

def render_perf_metrics() -> str:
    """Render *all* performance histograms and R5 outcome counters.

    Renders the shared ``prometheus_client`` registry, so the bounded R5
    counters (gateway requests, structured fallback, safety decisions) are
    exported alongside the histograms on the same endpoint.

    Returns an empty string when prometheus_client is not installed
    (all metrics are no-ops with no recorded values).
    """
    try:
        import prometheus_client
        from prometheus_client import REGISTRY

        return prometheus_client.generate_latest(REGISTRY).decode("utf-8")
    except ImportError:
        return ""
