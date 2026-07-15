"""L0-3 Legacy 问诊回合性能基线。

只测本地 API、数据库、Redis 和 fake Agent 编排开销，不代表真实模型 SLA。
"""

from __future__ import annotations

import logging
import math
import os
import time
from typing import Any

import pytest
from httpx import AsyncClient

from app.agents.registry import AgentRegistry
from app.db.session import reset_session_factory
from app.schemas.types import Stage
from tests._database_safety import validate_test_database_url
from tests.e2e.conftest import (
    FakeInquiryAgent,
    FakeSufficiencyAgent,
    cleanup_session_lock,
    cleanup_stream,
    create_session,
)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.performance,
    pytest.mark.asyncio(loop_scope="module"),
]
logger = logging.getLogger(__name__)


class _CountingAgent:
    def __init__(self, inner: Any) -> None:
        self._inner = inner
        self.calls = 0
        self.name = inner.name
        self.stage = inner.stage
        self.primary_sources = inner.primary_sources
        self.allow_cross_source = inner.allow_cross_source

    async def run(self, state: Any, trace_id: str) -> Any:
        self.calls += 1
        return await self._inner.run(state, trace_id)


def _percentile_nearest_rank(samples: list[float], percentile: float) -> float:
    ordered = sorted(samples)
    index = max(0, math.ceil(percentile * len(ordered)) - 1)
    return ordered[index]


async def test_legacy_message_round_performance_baseline(
    client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """20 个独立新会话的首轮消息应保持 2 次模型调用且无失败。"""
    import app.services.message as message_module

    inquiry = _CountingAgent(FakeInquiryAgent())
    sufficiency = _CountingAgent(FakeSufficiencyAgent())
    registry = AgentRegistry()
    registry.register(Stage.INQUIRY, inquiry)  # type: ignore[arg-type]
    registry.register(Stage.SUFFICIENCY, sufficiency)  # type: ignore[arg-type]

    original = message_module._default_inquiry_registry
    message_module._default_inquiry_registry = lambda: registry  # type: ignore[assignment]
    session_ids: list[str] = []
    durations_ms: list[float] = []
    failures = 0

    # Match the LangGraph benchmark and production by using SQLAlchemy's real
    # connection pool.  The fixture has already provisioned an isolated *_test
    # database; dispose the loop-bound pool before integration-fixture teardown.
    validate_test_database_url(os.environ["DB_URL"])
    await reset_session_factory()
    monkeypatch.delenv("XUANHU_ALLOW_DESTRUCTIVE_TESTS")
    try:
        for index in range(20):
            session_data = await create_session(client)
            session_id = session_data["session_id"]
            session_ids.append(session_id)
            started = time.perf_counter()
            response = await client.post(
                f"/api/v1/consult/sessions/{session_id}/messages",
                json={
                    "content": f"headache baseline round {index + 1}",
                    "role": "patient_proxy",
                },
                headers={
                    "X-Doctor-Id": "doctor_l0_perf",
                    "X-State-Version": "1",
                    "X-Idempotency-Key": f"l0-legacy-message-{index}",
                },
            )
            durations_ms.append((time.perf_counter() - started) * 1000)
            if response.status_code != 200:
                failures += 1

        p50 = _percentile_nearest_rank(durations_ms, 0.50)
        p95 = _percentile_nearest_rank(durations_ms, 0.95)
        maximum = max(durations_ms)
        total_calls = inquiry.calls + sufficiency.calls
        print(  # noqa: T201 - explicit performance runs must emit their measurements
            f"legacy_message_baseline p50_ms={p50:.2f} p95_ms={p95:.2f} max_ms={maximum:.2f}"
        )
        logger.info(
            "legacy_message_baseline rounds=20 calls=%d p50_ms=%.2f "
            "p95_ms=%.2f failures=%d token_usage=unavailable",
            total_calls,
            p50,
            p95,
            failures,
        )

        assert failures == 0
        assert inquiry.calls == 20
        assert sufficiency.calls == 20
        assert total_calls == 40
        assert p95 < 5000, f"p50_ms={p50:.2f} p95_ms={p95:.2f} max_ms={maximum:.2f}"
    finally:
        message_module._default_inquiry_registry = original  # type: ignore[assignment]
        for session_id in session_ids:
            await cleanup_stream(session_id)
            await cleanup_session_lock(session_id)
        await reset_session_factory()
