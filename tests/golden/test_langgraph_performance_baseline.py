"""Production-shaped LangGraph L3 orchestration baseline.

The model is deterministic and local, while FastAPI lifespan, PostgreSQL,
Redis, the shared Postgres checkpointer, command claims and Domain writes are
real.  This makes the result reproducible and gives the Legacy and LangGraph
baselines the same workload shape: 20 independent new sessions, one first
message per session.  It does not turn ordinary CI into a live-model cost/SLA
test.

The R7 default async production contract is exercised end-to-end: the message
POST is durably admitted with an HTTP 202 (command id + Location) even without
``Prefer: respond-async``, and the benchmark then polls the real command status
endpoint until ``succeeded``, so the mocked intake/question/classify calls
actually complete inside the lifespan-started supervised worker.
"""

from __future__ import annotations

import asyncio
import logging
import math
import os
import time
from typing import Any
from uuid import UUID

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


# Bounded per-round completion deadline. The worker polls every 0.5s and the
# local deterministic graph finishes in well under the p95 gate; 30s is a
# generous fail-closed ceiling for slow CI, never an unbounded wait.
_ROUND_COMPLETION_TIMEOUT_SECONDS = 30.0
# Short async polling interval for the bounded command-status poll.
_COMMAND_POLL_INTERVAL_SECONDS = 0.25


async def _await_command_succeeded(
    client: AsyncClient,
    location: str,
    *,
    timeout_seconds: float = _ROUND_COMPLETION_TIMEOUT_SECONDS,
    poll_interval_seconds: float = _COMMAND_POLL_INTERVAL_SECONDS,
) -> None:
    """Poll the real command-status endpoint until ``succeeded``, else fail closed.

    The lifespan (already entered by the caller) has started the real supervised
    worker, so reaching ``succeeded`` means the mocked intake/question/classify
    calls genuinely completed. ``queued``/``running`` keep polling; ``failed``
    and any unknown (ambiguous / dead-letter) status fail closed immediately;
    a monotonic deadline bounds the wait.
    """
    deadline = time.monotonic() + timeout_seconds
    while True:
        status_response = await client.get(location)
        assert status_response.status_code == 200, status_response.text
        status = status_response.json()["data"]
        state = status["status"]
        if state == "succeeded":
            return
        if state != "queued" and state != "running":
            raise AssertionError(
                f"command ended in non-success state: {state!r} (location={location} body={status_response.text})"
            )
        if time.monotonic() >= deadline:
            raise AssertionError(
                f"command did not reach succeeded within {timeout_seconds:.1f}s "
                f"(last_state={state!r} location={location} body={status_response.text})"
            )
        await asyncio.sleep(poll_interval_seconds)


async def test_langgraph_message_round_performance_baseline(
    monkeypatch: pytest.MonkeyPatch,
    enable_public_langgraph: None,
) -> None:
    """20 isolated async L3 rounds reach succeeded, asserting 202 admission shape.

    Each round: durable 202 admission (command id + Location), then bounded
    polling of the real command status until ``succeeded``. One intake call and
    one question-wording call per round still hold (with the 2.8 question retry).
    """

    del enable_public_langgraph
    gateway = _ObservedBaselineGateway()
    monkeypatch.setattr(
        langgraph_intake_module,
        "AgentRuntime",
        lambda: AgentRuntime(gateway),
    )
    durations_ms: list[float] = []

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
                    # R7 default async admission returns a durable 202 even
                    # without Prefer: respond-async. Assert the acceptance shape
                    # (command id + Location), not merely the status code.
                    assert response.status_code == 202, response.text
                    admitted = response.json()["data"]
                    command_id = admitted["command_id"]
                    UUID(command_id)
                    expected_location = f"/api/v1/consult/sessions/{session_id}/commands/{command_id}"
                    location = response.headers.get("Location")
                    assert location == expected_location, (location, expected_location)
                    assert admitted["status"] == "queued"
                    assert admitted["operation"] == "intake.message"
                    assert admitted["links"]["self"] == expected_location
                    # Poll the real command status until the supervised worker
                    # reaches succeeded; measure end-to-end (POST through done).
                    await _await_command_succeeded(client, location)
                    durations_ms.append((time.perf_counter() - started) * 1000)
    finally:
        await reset_session_factory()

    p50 = _percentile_nearest_rank(durations_ms, 0.50)
    p95 = _percentile_nearest_rank(durations_ms, 0.95)
    maximum = max(durations_ms)
    print(  # noqa: T201 - explicit performance runs must emit their measurements
        f"langgraph_async_message_baseline e2e_p50_ms={p50:.2f} e2e_p95_ms={p95:.2f} e2e_max_ms={maximum:.2f}"
    )
    logger.info(
        "langgraph_async_message_baseline rounds=20 intake_calls=%d question_calls=%d "
        "classify_calls=%d e2e_p50_ms=%.2f e2e_p95_ms=%.2f e2e_max_ms=%.2f "
        "prompt_tokens=%d completion_tokens=%d total_tokens=%d",
        gateway.intake_calls,
        gateway.question_model_calls,
        gateway.classify_calls,
        p50,
        p95,
        maximum,
        gateway.prompt_tokens,
        gateway.completion_tokens,
        gateway.total_tokens,
    )

    assert gateway.intake_calls == 20
    # 2.8 的 SINGLE_QUESTION_INVALID 重试在本 fake 下不会触发：_QUESTION_TEXT_BY_DIMENSION
    # 的题文末句必含 selected_dimension 规范关键词（_question_targets_dimension 命中），
    # 因此每会话恰 1 次 question 模型调用（20 = 20 × 1），与同步/异步路径一致。若改用
    # 不含维度关键词的静态复读问句，重试会让 question_model_calls 膨胀到 40。
    assert gateway.question_model_calls == 20
    assert gateway.classify_calls == 20
    # 60 = intake 20 + question 20 + classify 20；每次调用 usage 17/5/22。
    assert gateway.prompt_tokens == 1020
    assert gateway.completion_tokens == 300
    assert gateway.total_tokens == 1320
    assert p95 < 5000, f"p50_ms={p50:.2f} p95_ms={p95:.2f} max_ms={maximum:.2f}"
