from __future__ import annotations

import asyncio
import json
from datetime import timedelta
from uuid import UUID, uuid4

import psycopg
import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import delete, func, update

from app.agent_runtime.repository import PostgresDomainRepository, RepositoryError, RepositoryErrorCode
from app.core.config import get_settings
from app.core.redis import get_redis
from app.db.session import get_session_factory
from app.models.consult import ConsultSession
from app.models.domain import GraphRun, OutboxEvent
from app.services.events import (
    EventService,
    session_event_dedupe_key,
    session_event_dedupe_order_key,
    session_event_stream_key,
)
from app.services.outbox_publisher import OutboxPublisher

pytestmark = [pytest.mark.integration, pytest.mark.asyncio(loop_scope="session")]


async def test_00_outbox_dead_letter_migration_roundtrip() -> None:
    config = Config("alembic.ini")
    database_url = get_settings().database_url
    try:
        command.downgrade(config, "20260712_0007")
        with psycopg.connect(database_url) as connection:
            assert connection.execute(
                "SELECT 1 FROM information_schema.columns "
                "WHERE table_name = 'outbox_events' AND column_name = 'dead_lettered_at'"
            ).fetchone() is None
        command.upgrade(config, "head")
        with psycopg.connect(database_url) as connection:
            assert connection.execute(
                "SELECT 1 FROM information_schema.columns "
                "WHERE table_name = 'outbox_events' AND column_name = 'dead_lettered_at'"
            ).fetchone() == (1,)
    finally:
        command.upgrade(config, "head")


async def _seed_outbox(event_type: str, payload: dict[str, object], *, attempt_count: int = 0) -> tuple[UUID, UUID]:
    session_id = uuid4()
    run_id = uuid4()
    event_id = uuid4()
    factory = get_session_factory()
    async with factory() as db, db.begin():
        # A worker database is shared by several integration modules.  Publisher
        # assertions must start from an explicitly empty queue instead of
        # claiming pending rows intentionally produced by an earlier test.
        await db.execute(delete(OutboxEvent))
        db.add(
            ConsultSession(
                id=session_id,
                patient_info={},
                current_stage="inquiry",
                status="active",
                agent_runtime="langgraph",
                rollback_counts={},
                state_version=1,
                recovery_status="normal",
            )
        )
        await db.flush()
        db.add(
            GraphRun(
                id=run_id,
                session_id=session_id,
                graph_version="integration-v1",
                command_id=f"command-{event_id}",
                input_state_version=1,
                status="completed",
            )
        )
        await db.flush()
        db.add(
            OutboxEvent(
                id=event_id,
                event_type=event_type,
                session_id=session_id,
                graph_run_id=run_id,
                state_version=1,
                trace_id="trace:integration",
                payload=payload,
                status="pending",
                attempt_count=attempt_count,
            )
        )
    return event_id, session_id


async def _cleanup(session_id: UUID) -> None:
    factory = get_session_factory()
    async with factory() as db, db.begin():
        await db.execute(delete(ConsultSession).where(ConsultSession.id == session_id))
    redis = await get_redis()
    await redis.delete(
        session_event_stream_key(str(session_id)),
        session_event_dedupe_key(str(session_id)),
        session_event_dedupe_order_key(str(session_id)),
    )


async def test_real_postgres_claim_redis_publish_and_ack() -> None:
    message_id = uuid4()
    event_id, session_id = await _seed_outbox(
        "intake.message_created.v1",
        {"message_id": str(message_id), "role": "doctor", "stage": "inquiry"},
    )
    try:
        repository = PostgresDomainRepository(get_session_factory())
        result = await OutboxPublisher(repository, EventService(), worker_id="integration-a").run_once()

        assert result.published == 1
        async with get_session_factory()() as db:
            row = await db.get(OutboxEvent, event_id)
            assert row is not None
            assert row.status == "published"
            assert row.published_at is not None
        redis = await get_redis()
        rows = await redis.xrange(session_event_stream_key(str(session_id)))
        assert len(rows) == 2
        assert rows[0][1]["event_type"] == "message.created"
        assert str(message_id) in rows[0][1]["payload"]
        assert rows[1][1]["event_type"] == "agent.started"
    finally:
        await _cleanup(session_id)


async def test_published_event_reaches_two_independent_sse_clients_once_each() -> None:
    event_id, session_id = await _seed_outbox("domain.state_committed.v1", {})
    client_a = EventService().iter_sse(
        str(session_id),
        last_event_id="0-0",
        heartbeat_interval_seconds=0.01,
    )
    client_b = EventService().iter_sse(
        str(session_id),
        last_event_id="0-0",
        heartbeat_interval_seconds=0.01,
    )
    try:
        result = await OutboxPublisher(
            PostgresDomainRepository(get_session_factory()),
            EventService(),
            worker_id="integration-sse-broadcast",
        ).run_once()
        assert result.published == 1

        first_a, first_b = await asyncio.wait_for(
            asyncio.gather(anext(client_a), anext(client_b)),
            timeout=2,
        )
        assert first_a == first_b
        assert first_a.count("event: agent.finished\n") == 1
        assert "id: " in first_a
        payload_line = next(line for line in first_a.splitlines() if line.startswith("data: "))
        payload = json.loads(payload_line.removeprefix("data: "))
        assert payload["source_event_id"] == str(event_id)
        assert payload["agent_name"] == "domain_commit"

        next_a, next_b = await asyncio.wait_for(
            asyncio.gather(anext(client_a), anext(client_b)),
            timeout=2,
        )
        assert next_a.startswith("event: heartbeat\n")
        assert next_b.startswith("event: heartbeat\n")
        assert str(event_id) not in next_a
        assert str(event_id) not in next_b

        redis = await get_redis()
        rows = await redis.xrange(session_event_stream_key(str(session_id)))
        assert len(rows) == 1
        assert rows[0][1]["event_type"] == "agent.finished"
    finally:
        await client_a.aclose()
        await client_b.aclose()
        await _cleanup(session_id)


async def test_real_redis_dedupe_state_is_capped_and_uses_a_sliding_ttl() -> None:
    session_id = str(uuid4())
    stream_key = session_event_stream_key(session_id)
    dedupe_key = session_event_dedupe_key(session_id)
    order_key = session_event_dedupe_order_key(session_id)
    redis = await get_redis()
    service = EventService(redis, dedupe_ttl_seconds=5)
    try:
        results = []
        for index in range(100):
            results.append(
                await service.append_session_event_once(
                    session_id,
                    "agent.finished",
                    {"agent_name": "bounded-test"},
                    dedupe_id=f"bounded:{index}",
                    maxlen=7,
                )
            )

        assert all(result.deduplicated is False for result in results)
        assert await redis.hlen(dedupe_key) == 7
        assert await redis.llen(order_key) == 7
        assert await redis.lrange(order_key, 0, -1) == [f"bounded:{index}" for index in range(93, 100)]
        assert await redis.hexists(dedupe_key, "bounded:0") is False
        assert await redis.hexists(dedupe_key, "bounded:99") is True
        assert 0 < await redis.ttl(dedupe_key) <= 5
        assert 0 < await redis.ttl(order_key) <= 5

        stream_length = await redis.xlen(stream_key)
        await redis.expire(dedupe_key, 1)
        await redis.expire(order_key, 1)
        replay = await service.append_session_event_once(
            session_id,
            "agent.finished",
            {"agent_name": "bounded-test"},
            dedupe_id="bounded:99",
            maxlen=7,
        )
        assert replay.deduplicated is True
        assert replay.event_id == results[-1].event_id
        assert await redis.xlen(stream_key) == stream_length
        assert await redis.ttl(dedupe_key) > 1
        assert await redis.ttl(order_key) > 1
    finally:
        await redis.delete(stream_key, dedupe_key, order_key)


async def test_real_redis_replay_repairs_legacy_unbounded_dedupe_hash_without_republishing() -> None:
    session_id = str(uuid4())
    stream_key = session_event_stream_key(session_id)
    dedupe_key = session_event_dedupe_key(session_id)
    order_key = session_event_dedupe_order_key(session_id)
    redis = await get_redis()
    service = EventService(redis, dedupe_ttl_seconds=5)
    try:
        await redis.hset(dedupe_key, mapping={f"legacy:{index}": f"{index + 1}-0" for index in range(50)})

        replay = await service.append_session_event_once(
            session_id,
            "agent.finished",
            {"agent_name": "legacy-repair-test"},
            dedupe_id="legacy:49",
            maxlen=7,
        )

        assert replay.deduplicated is True
        assert replay.event_id == "50-0"
        assert await redis.xlen(stream_key) == 0
        assert await redis.hlen(dedupe_key) == 7
        assert await redis.llen(order_key) == 1
        assert await redis.hexists(dedupe_key, "legacy:49") is True
        assert 0 < await redis.ttl(dedupe_key) <= 5
        assert 0 < await redis.ttl(order_key) <= 5
    finally:
        await redis.delete(stream_key, dedupe_key, order_key)


class _FailAckOnceRepository(PostgresDomainRepository):
    async def acknowledge(self, event_id: UUID, *, worker_id: str) -> bool:
        del event_id, worker_id
        raise RepositoryError(RepositoryErrorCode.TRANSACTION_FAILED)


async def test_publish_after_ack_crash_is_deduplicated_on_lease_recovery() -> None:
    event_id, session_id = await _seed_outbox(
        "advance.command_started.v1",
        {"from_stage": "inquiry", "to_stage": "syndrome"},
    )
    factory = get_session_factory()
    try:
        first = OutboxPublisher(
            _FailAckOnceRepository(factory),
            EventService(),
            worker_id="integration-crash",
            lease_seconds=30,
        )
        assert (await first.run_once()).ownership_lost == 1

        async with factory() as db, db.begin():
            await db.execute(
                update(OutboxEvent)
                .where(OutboxEvent.id == event_id)
                .values(leased_until=func.now() - timedelta(seconds=1))
            )

        second = OutboxPublisher(
            PostgresDomainRepository(factory),
            EventService(),
            worker_id="integration-recovery",
        )
        assert (await second.run_once()).published == 1

        redis = await get_redis()
        rows = await redis.xrange(session_event_stream_key(str(session_id)))
        assert [row[1]["event_type"] for row in rows] == ["stage.changed", "agent.started"]
        async with factory() as db:
            row = await db.get(OutboxEvent, event_id)
            assert row is not None
            assert row.status == "published"
            assert row.attempt_count == 2
    finally:
        await _cleanup(session_id)


async def test_real_postgres_max_attempts_is_durable_dlq_and_health_visible() -> None:
    event_id, session_id = await _seed_outbox("unknown.event.v1", {}, attempt_count=2)
    factory = get_session_factory()
    try:
        repository = PostgresDomainRepository(factory)
        result = await OutboxPublisher(
            repository,
            EventService(),
            worker_id="integration-dlq",
            max_attempts=3,
        ).run_once()

        assert result.dead_lettered == 1
        async with factory() as db:
            row = await db.get(OutboxEvent, event_id)
            assert row is not None
            assert row.status == "dead_letter"
            assert row.dead_lettered_at is not None
            assert row.last_error_code == "PUBLISH_REJECTED"
        health = await repository.get_outbox_health()
        assert health.dead_letter_count >= 1
    finally:
        await _cleanup(session_id)
