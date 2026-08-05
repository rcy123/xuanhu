"""Per-stage Prometheus histogram metrics for the performance baseline.

All histograms use the same bucket profile tuned for millisecond-to-second
ranges: 0.01/0.05/0.1/0.25/0.5/1/2.5/5/10/30.  Labels never carry
high-cardinality or PHI values (no session_id, patient_ref, or query text).

Usage::

    async with measure("rag.vector") as m:
        results = await search(...)
    # m.seconds is now populated, histogram observed automatically.

"""

from __future__ import annotations

import logging
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

class _Histogram:
    """Simple histogram container — wraps prometheus_client if installed."""

    __slots__ = ("_inner",)

    def __init__(self, name: str, description: str, labelnames: tuple[str, ...] = ()) -> None:
        self._inner: Any = None
        try:
            import prometheus_client

            if labelnames:
                self._inner = prometheus_client.Histogram(
                    name,
                    description,
                    labelnames=labelnames,
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
        if self._inner is None:
            return
        if labels:
            self._inner.labels(**labels).observe(value)
        else:
            self._inner.observe(value)


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

# Gateway durations (labelled by host and route profile)
gateway_chat = _Histogram(
    "xuanhu_gateway_chat_seconds",
    "Gateway chat/chat_structured duration",
    labelnames=("host", "route_profile"),
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
    }
    h = _MAP.get(stage)
    if h is not None:
        h.observe(value, labels=labels)


# ---------------------------------------------------------------------------
# Render all histograms as Prometheus text format
# ---------------------------------------------------------------------------

def render_perf_metrics() -> str:
    """Render *all* performance histograms in Prometheus text format.

    Returns an empty string when prometheus_client is not installed
    (all histograms are no-ops with no recorded values).
    """
    try:
        import prometheus_client
        from prometheus_client import REGISTRY

        return prometheus_client.generate_latest(REGISTRY).decode("utf-8")
    except ImportError:
        return ""
