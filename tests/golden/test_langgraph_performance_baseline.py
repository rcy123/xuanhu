"""Production-shaped LangGraph L3 orchestration baseline.

The model is deterministic and local, while FastAPI lifespan, PostgreSQL,
Redis, the shared Postgres checkpointer, command claims and Domain writes are
real.  This makes the result reproducible and gives the Legacy and LangGraph
baselines the same workload shape: 20 independent new sessions, one first
message per session.  It does not turn ordinary CI into a live-model cost/SLA
test.
"""

from __future__ import annotations

import logging
import math
import os
import time
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from pydantic import BaseModel

import app.services.langgraph_intake as langgraph_intake_module
from app.agent_runtime.runtime import AgentRuntime
from app.core.gateway import ModelTokenUsage, StructuredChatResponse
from app.db.session import reset_session_factory
from app.main import app
from tests._database_safety import validate_test_database_url
from tests.test_l3_5_intake_subgraph import _E2EFakeGateway

pytestmark = [
    pytest.mark.integration,
    pytest.mark.performance,
    pytest.mark.asyncio(loop_scope="module"),
]
logger = logging.getLogger(__name__)


class _ObservedBaselineGateway(_E2EFakeGateway):
    """Return response-side provenance while retaining the countable fake."""

    def __init__(self) -> None:
        super().__init__("incomplete")
        self.prompt_tokens = 0
        self.completion_tokens = 0
        self.total_tokens = 0

    async def chat_structured_observed(
        self,
        messages: list[dict[str, Any]],
        output_schema: type[BaseModel],
        **kwargs: Any,
    ) -> StructuredChatResponse:
        output = await self.chat_structured(messages, output_schema, **kwargs)
        usage = ModelTokenUsage(prompt_tokens=17, completion_tokens=5, total_tokens=22)
        self.prompt_tokens += usage.prompt_tokens
        self.completion_tokens += usage.completion_tokens
        self.total_tokens += usage.total_tokens
        return StructuredChatResponse(
            output=output_schema.model_validate(output),
            model_actual="baseline-served-model.v1",
            usage=usage,
        )


def _percentile_nearest_rank(samples: list[float], percentile: float) -> float:
    ordered = sorted(samples)
    index = max(0, math.ceil(percentile * len(ordered)) - 1)
    return ordered[index]


async def test_langgraph_message_round_performance_baseline(
    monkeypatch: pytest.MonkeyPatch,
    enable_public_langgraph: None,
) -> None:
    """20 isolated L3 rounds use one intake call and one question-wording call."""

    del enable_public_langgraph
    gateway = _ObservedBaselineGateway()
    monkeypatch.setattr(
        langgraph_intake_module,
        "AgentRuntime",
        lambda: AgentRuntime(gateway),
    )
    durations_ms: list[float] = []
    failures = 0

    # The integration safety sentinel selects NullPool because the full suite
    # intentionally spans several event loops.  This performance module uses
    # one loop, and production uses the configured SQLAlchemy connection pool.
    # Keep the fixture-provisioned isolated *_test database, but benchmark the
    # production pool and dispose it on this same loop before fixture teardown.
    validate_test_database_url(os.environ["DB_URL"])
    await reset_session_factory()
    monkeypatch.delenv("XUANHU_ALLOW_DESTRUCTIVE_TESTS")
    try:
        # ASGITransport does not trigger lifespan itself.  Enter it explicitly
        # so the benchmark covers the shared graph/checkpointer lifecycle.
        async with app.router.lifespan_context(app):
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                for index in range(20):
                    created = await client.post(
                        "/api/v1/consult/sessions",
                        json={"agent_runtime": "langgraph"},
                        headers={
                            "X-Doctor-Id": "doctor_l0_langgraph_perf",
                            "X-Idempotency-Key": f"l0-langgraph-create-{index}",
                        },
                    )
                    assert created.status_code == 201
                    session_id = created.json()["data"]["session_id"]

                    started = time.perf_counter()
                    response = await client.post(
                        f"/api/v1/consult/sessions/{session_id}/messages",
                        json={
                            "content": f"headache baseline round {index + 1}",
                            "role": "patient_proxy",
                        },
                        headers={
                            "X-Doctor-Id": "doctor_l0_langgraph_perf",
                            "X-State-Version": "1",
                            "X-Idempotency-Key": f"l0-langgraph-message-{index}",
                        },
                    )
                    durations_ms.append((time.perf_counter() - started) * 1000)
                    if response.status_code != 200:
                        failures += 1
    finally:
        await reset_session_factory()

    p50 = _percentile_nearest_rank(durations_ms, 0.50)
    p95 = _percentile_nearest_rank(durations_ms, 0.95)
    maximum = max(durations_ms)
    print(  # noqa: T201 - explicit performance runs must emit their measurements
        f"langgraph_message_baseline p50_ms={p50:.2f} p95_ms={p95:.2f} max_ms={maximum:.2f}"
    )
    logger.info(
        "langgraph_message_baseline rounds=20 intake_calls=%d question_calls=%d "
        "p50_ms=%.2f p95_ms=%.2f failures=%d prompt_tokens=%d "
        "completion_tokens=%d total_tokens=%d",
        gateway.intake_calls,
        gateway.question_model_calls,
        p50,
        p95,
        failures,
        gateway.prompt_tokens,
        gateway.completion_tokens,
        gateway.total_tokens,
    )

    assert failures == 0
    assert gateway.intake_calls == 20
    assert gateway.question_model_calls == 20
    assert gateway.prompt_tokens == 680
    assert gateway.completion_tokens == 200
    assert gateway.total_tokens == 880
    assert p95 < 5000, f"p50_ms={p50:.2f} p95_ms={p95:.2f} max_ms={maximum:.2f}"
