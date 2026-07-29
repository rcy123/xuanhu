"""PostgreSQL acceptance for the global runtime-switch audit ledger."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime

import pytest
import pytest_asyncio
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.exceptions import RuntimeSwitchAuditMismatchError
from app.models.audit import AuditEvent
from app.models.consult import ConsultSession
from app.schemas.session import SessionCreateRequest
from app.services.runtime_switch_audit import (
    RUNTIME_SWITCH_EVENT,
    PostgresRuntimeSwitchAuditRepository,
    RuntimeSwitchAuditConflict,
    RuntimeSwitchAuditService,
    RuntimeSwitchRecord,
)
from app.services.session import SessionService

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


async def test_runtime_switch_audit_round_trip_is_global_allowlisted_and_idempotent(
    db: AsyncSession,
) -> None:
    await db.execute(delete(AuditEvent).where(AuditEvent.event_type == RUNTIME_SWITCH_EVENT))
    await db.commit()
    record = RuntimeSwitchRecord(
        from_runtime="legacy",
        to_runtime="langgraph",
        operator="integration-release-bot",
        reason="approved isolated integration rollout",
        deployment_id="integration-deploy-0001",
        timestamp=datetime.now(UTC),
    )
    service = RuntimeSwitchAuditService(PostgresRuntimeSwitchAuditRepository(db))

    stored, replayed = await service.record_switch(record, configured_runtime="langgraph")
    await db.commit()
    status = await service.status("langgraph")
    replays = [await service.record_switch(record, configured_runtime="langgraph") for _ in range(6)]

    rows = (await db.scalars(select(AuditEvent).where(AuditEvent.event_type == RUNTIME_SWITCH_EVENT))).all()
    assert stored == record
    assert replayed is False
    assert status.last_switch_at == record.timestamp
    assert all(replay == record and was_replayed for replay, was_replayed in replays)
    assert len(rows) == 1
    row = rows[0]
    assert row.session_id is None
    assert row.actor_type == "system"
    assert row.actor_id == record.operator
    assert row.trace_id == record.deployment_id
    assert row.payload == record.model_dump(mode="json")
    assert "patient" not in str(row.payload).lower()
    await db.execute(delete(AuditEvent).where(AuditEvent.event_type == RUNTIME_SWITCH_EVENT))
    await db.commit()


async def test_runtime_switch_audit_serializes_concurrent_deployments(
    db: AsyncSession,
) -> None:
    from app.db.session import get_session_factory

    await db.execute(delete(AuditEvent).where(AuditEvent.event_type == RUNTIME_SWITCH_EVENT))
    await db.commit()
    factory = get_session_factory()
    base = RuntimeSwitchRecord(
        from_runtime="legacy",
        to_runtime="langgraph",
        operator="integration-release-bot",
        reason="approved concurrent integration rollout",
        deployment_id="integration-concurrent-same",
        timestamp=datetime.now(UTC),
    )

    async def persist(record: RuntimeSwitchRecord) -> tuple[RuntimeSwitchRecord, bool]:
        async with factory() as session, session.begin():
            return await RuntimeSwitchAuditService(PostgresRuntimeSwitchAuditRepository(session)).record_switch(
                record, configured_runtime="langgraph"
            )

    identical = await asyncio.gather(persist(base), persist(base))
    assert sorted(replayed for _, replayed in identical) == [False, True]
    assert (
        await db.scalar(
            select(func.count()).select_from(AuditEvent).where(AuditEvent.event_type == RUNTIME_SWITCH_EVENT)
        )
        == 1
    )

    await db.execute(delete(AuditEvent).where(AuditEvent.event_type == RUNTIME_SWITCH_EVENT))
    await db.commit()
    divergent = [
        base.model_copy(update={"deployment_id": "integration-concurrent-a"}),
        base.model_copy(update={"deployment_id": "integration-concurrent-b"}),
    ]
    outcomes = await asyncio.gather(
        *(persist(record) for record in divergent),
        return_exceptions=True,
    )
    assert sum(isinstance(result, tuple) for result in outcomes) == 1
    assert sum(isinstance(result, RuntimeSwitchAuditConflict) for result in outcomes) == 1
    assert (
        await db.scalar(
            select(func.count()).select_from(AuditEvent).where(AuditEvent.event_type == RUNTIME_SWITCH_EVENT)
        )
        == 1
    )

    await db.execute(delete(AuditEvent).where(AuditEvent.event_type == RUNTIME_SWITCH_EVENT))
    await db.commit()


async def test_default_session_creation_fails_closed_on_audit_mismatch_but_explicit_runtime_is_stable(
    db: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AGENT_RUNTIME_VERSION", "legacy")
    get_settings.cache_clear()
    await db.execute(delete(AuditEvent).where(AuditEvent.event_type == RUNTIME_SWITCH_EVENT))
    await db.commit()
    try:
        service = RuntimeSwitchAuditService(PostgresRuntimeSwitchAuditRepository(db))
        await service.record_switch(
            RuntimeSwitchRecord(
                from_runtime="legacy",
                to_runtime="langgraph",
                operator="integration-release-bot",
                reason="approved isolated integration rollout",
                deployment_id="integration-deploy-0002",
                timestamp=datetime.now(UTC),
            ),
            configured_runtime="langgraph",
        )
        await db.commit()

        before = await db.scalar(select(func.count()).select_from(ConsultSession))
        with pytest.raises(RuntimeSwitchAuditMismatchError, match="发布审计"):
            await SessionService(db).create_session(
                SessionCreateRequest(),
                doctor_id="doctor-runtime-audit",
                trace_id="runtime-audit-default-mismatch",
            )
        after = await db.scalar(select(func.count()).select_from(ConsultSession))
        assert after == before

        # An explicit runtime is not a global feature-flag transition.  It is
        # still governed by the public canary flag at the HTTP boundary and
        # must not be silently rewritten by a later default switch.
        created = await SessionService(db).create_session(
            SessionCreateRequest(agent_runtime="legacy"),
            doctor_id="doctor-runtime-audit",
            trace_id="runtime-audit-explicit-runtime",
        )
        stored = await db.get(ConsultSession, uuid.UUID(created.session_id))
        assert stored is not None
        assert stored.agent_runtime == "legacy"
    finally:
        await db.rollback()
        await db.execute(delete(AuditEvent).where(AuditEvent.event_type == RUNTIME_SWITCH_EVENT))
        await db.commit()
        get_settings.cache_clear()
