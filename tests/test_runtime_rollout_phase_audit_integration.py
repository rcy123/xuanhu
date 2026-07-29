"""PostgreSQL round-trip for the durable rollout-phase audit ledger."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.audit import AuditEvent
from app.services.runtime_rollout_phase_audit import (
    ROLLOUT_PHASE_EVENT,
    PostgresRuntimeRolloutPhaseAuditRepository,
    RuntimeRolloutPhaseAuditService,
    RuntimeRolloutPhaseRecord,
)

pytestmark = [pytest.mark.integration, pytest.mark.asyncio(loop_scope="module")]


@pytest_asyncio.fixture(loop_scope="module")
async def db() -> AsyncIterator[AsyncSession]:
    from app.db.session import get_session_factory, reset_session_factory

    await reset_session_factory()
    factory = get_session_factory()
    try:
        async with factory() as session:
            yield session
    finally:
        await reset_session_factory()


async def test_phase_audit_round_trip_is_global_allowlisted_and_idempotent(
    db: AsyncSession,
) -> None:
    await db.execute(delete(AuditEvent).where(AuditEvent.event_type == ROLLOUT_PHASE_EVENT))
    await db.commit()
    record = RuntimeRolloutPhaseRecord(
        from_phase="legacy",
        to_phase="full",
        runtime="langgraph",
        runtime_switch_deployment_id="integration-runtime-deploy",
        operator="integration-release-bot",
        reason="approved isolated integration full transition",
        deployment_id="integration-phase-deploy",
        timestamp=datetime.now(UTC) - timedelta(hours=2),
    )
    service = RuntimeRolloutPhaseAuditService(PostgresRuntimeRolloutPhaseAuditRepository(db))

    stored, replayed = await service.record_transition(
        record,
        configured_phase="full",
        configured_runtime="langgraph",
        runtime_switch_deployment_id="integration-runtime-deploy",
    )
    await db.commit()
    replay, was_replayed = await service.record_transition(
        record.model_copy(update={"timestamp": record.timestamp + timedelta(minutes=5)}),
        configured_phase="full",
        configured_runtime="langgraph",
        runtime_switch_deployment_id="integration-runtime-deploy",
    )
    status = await service.status(
        configured_phase="full",
        configured_runtime="langgraph",
        runtime_switch_deployment_id="integration-runtime-deploy",
        expected_phase_deployment_id="integration-phase-deploy",
    )

    row = await db.scalar(select(AuditEvent).where(AuditEvent.event_type == ROLLOUT_PHASE_EVENT))
    assert stored == replay == record
    assert replayed is False
    assert was_replayed is True
    assert status.status == "ok"
    assert status.full_entered_at == record.timestamp
    assert row is not None
    assert row.session_id is None
    assert row.actor_type == "system"
    assert row.actor_id == record.operator
    assert row.trace_id == record.deployment_id
    assert row.created_at == record.timestamp
    assert row.payload == record.model_dump(mode="json")
    assert (
        await db.scalar(
            select(func.count()).select_from(AuditEvent).where(AuditEvent.event_type == ROLLOUT_PHASE_EVENT)
        )
        == 1
    )
    await db.execute(delete(AuditEvent).where(AuditEvent.event_type == ROLLOUT_PHASE_EVENT))
    await db.commit()
