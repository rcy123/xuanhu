"""R6-A async-command real-PostgreSQL/Redis integration tests.

Requires the guarded destructive TEST_* database/redis services (the
``integration`` marker). Exercises: migration roundtrip, enqueue
replay/conflict/session-busy, SKIP LOCKED multi-worker claiming, expired-lease
reclaim, owner-token fencing (including stale-owner-after-takeover), retry and
max-attempts, same-transaction Outbox rows, worker dispatch, Outbox->Redis
publication with append-once dedupe, and PHI privacy across Outbox/SSE/status.
"""

from __future__ import annotations

import asyncio
import json
from datetime import timedelta
from typing import Any
from uuid import UUID, uuid4

import httpx
import psycopg
import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import delete, func, select, update

from app.agent_runtime.async_command import (
    STATUS_FAILED,
    STATUS_RUNNING,
    STATUS_SUCCEEDED,
    PostgresAsyncCommandRepository,
)
from app.agent_runtime.async_command_worker import (
    AsyncCommandContext,
    AsyncCommandWorker,
    CommandFailureError,
    CommandSuccess,
)
from app.agent_runtime.repository import PostgresDomainRepository
from app.core.config import get_settings
from app.core.exceptions import IdempotencyConflictError, SessionBusyError, SessionNotFoundError
from app.core.redis import get_redis
from app.db.session import get_session_factory
from app.main import app
from app.models.async_command import AsyncCommand
from app.models.consult import ConsultSession
from app.models.domain import OutboxEvent
from app.services.events import (
    EventService,
    session_event_dedupe_key,
    session_event_dedupe_order_key,
    session_event_stream_key,
)
from app.services.outbox_publisher import OutboxPublisher

pytestmark = [pytest.mark.integration, pytest.mark.asyncio(loop_scope="session")]

# Must be one of the finite R6-B operation allowlist.
OPERATION = "intake.message"


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


async def _seed_session() -> UUID:
    session_id = uuid4()
    factory = get_session_factory()
    async with factory() as db, db.begin():
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
    return session_id


async def _enqueue(session_id: UUID, *, key: str, payload: dict[str, Any]) -> UUID:
    repo = PostgresAsyncCommandRepository(get_session_factory())
    ref = await repo.enqueue(
        session_id=session_id,
        operation=OPERATION,
        idempotency_key=key,
        request_payload=payload,
    )
    return ref.command_id


async def _command_row(command_id: UUID):
    async with get_session_factory()() as db:
        return await db.get(AsyncCommand, command_id)


async def _outbox_rows(session_id: UUID) -> list[OutboxEvent]:
    async with get_session_factory()() as db:
        return list(
            (
                await db.scalars(
                    select(OutboxEvent).where(OutboxEvent.session_id == session_id).order_by(OutboxEvent.created_at)
                )
            ).all()
        )


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


async def _ok_handler() -> Any:
    async def handler(ctx: AsyncCommandContext) -> CommandSuccess:
        del ctx
        return CommandSuccess(http_status=200, result_payload={"done": True})

    return handler


# ---------------------------------------------------------------------------
# migration roundtrip (must run first, before async rows exist)
# ---------------------------------------------------------------------------


async def test_00_migration_roundtrip() -> None:
    config = Config("alembic.ini")
    database_url = get_settings().database_url
    # Seed real lifecycle data (a queued command + its async-command outbox rows
    # with NULL graph_run_id) so the downgrade must remove them before restoring
    # graph_run_id NOT NULL.
    session_id = await _seed_session()
    try:
        await _enqueue(session_id, key="downgrade-key", payload={"a": 1})
        assert await _outbox_rows(session_id)  # async-command outbox rows exist

        command.downgrade(config, "20260729_0015")
        with psycopg.connect(database_url) as connection:
            assert (
                connection.execute(
                    "SELECT 1 FROM information_schema.tables WHERE table_name = 'async_commands'"
                ).fetchone()
                is None
            )
            assert connection.execute(
                "SELECT is_nullable FROM information_schema.columns "
                "WHERE table_name = 'outbox_events' AND column_name = 'graph_run_id'"
            ).fetchone() == ("NO",)
            remaining = connection.execute(
                "SELECT COUNT(*) FROM outbox_events WHERE event_type LIKE 'async_command.%'"
            ).fetchone()[0]
            assert remaining == 0

        command.upgrade(config, "head")
        with psycopg.connect(database_url) as connection:
            assert connection.execute(
                "SELECT 1 FROM information_schema.tables WHERE table_name = 'async_commands'"
            ).fetchone() == (1,)
            assert connection.execute(
                "SELECT is_nullable FROM information_schema.columns "
                "WHERE table_name = 'outbox_events' AND column_name = 'graph_run_id'"
            ).fetchone() == ("YES",)
    finally:
        command.upgrade(config, "head")
        await _cleanup(session_id)


# ---------------------------------------------------------------------------
# enqueue semantics
# ---------------------------------------------------------------------------


async def test_enqueue_replay_returns_same_command() -> None:
    session_id = await _seed_session()
    try:
        first = await _enqueue(session_id, key="k1", payload={"a": 1})
        second = await _enqueue(session_id, key="k1", payload={"a": 1})
        assert second == first
        # Only one command row and one queued outbox row.
        rows = await _outbox_rows(session_id)
        queued = [r for r in rows if r.event_type == "async_command.queued.v1"]
        assert len(queued) == 1
    finally:
        await _cleanup(session_id)


async def test_enqueue_conflict_same_key_different_payload() -> None:
    session_id = await _seed_session()
    try:
        await _enqueue(session_id, key="k2", payload={"a": 1})
        repo = PostgresAsyncCommandRepository(get_session_factory())
        with pytest.raises(IdempotencyConflictError):
            await repo.enqueue(
                session_id=session_id,
                operation=OPERATION,
                idempotency_key="k2",
                request_payload={"a": 2},
            )
    finally:
        await _cleanup(session_id)


async def test_enqueue_session_busy_while_active() -> None:
    session_id = await _seed_session()
    try:
        await _enqueue(session_id, key="k3", payload={"a": 1})
        repo = PostgresAsyncCommandRepository(get_session_factory())
        with pytest.raises(SessionBusyError):
            await repo.enqueue(
                session_id=session_id,
                operation=OPERATION,
                idempotency_key="k4",
                request_payload={"b": 2},
            )
    finally:
        await _cleanup(session_id)


async def test_enqueue_different_sessions_may_each_be_active() -> None:
    session_a = await _seed_session()
    session_b = await _seed_session()
    try:
        cmd_a = await _enqueue(session_a, key="ka", payload={})
        cmd_b = await _enqueue(session_b, key="kb", payload={})
        assert cmd_a != cmd_b
    finally:
        await _cleanup(session_a)
        await _cleanup(session_b)


async def test_enqueue_missing_session_not_found() -> None:
    repo = PostgresAsyncCommandRepository(get_session_factory())
    with pytest.raises(SessionNotFoundError):
        await repo.enqueue(
            session_id=uuid4(),
            operation=OPERATION,
            idempotency_key="kx",
            request_payload={},
        )


# ---------------------------------------------------------------------------
# get_status scoping
# ---------------------------------------------------------------------------


async def test_get_status_is_session_scoped() -> None:
    session_id = await _seed_session()
    other_session = await _seed_session()
    try:
        command_id = await _enqueue(session_id, key="ks", payload={"secret": "PHI"})
        repo = PostgresAsyncCommandRepository(get_session_factory())
        own = await repo.get_status(session_id, command_id)
        assert own is not None
        assert own.command_id == command_id
        assert own.status == "queued"
        assert own.attempt_count == 0
        # Private payload and digests are never projected into status.
        assert "secret" not in own.model_dump().values()
        cross = await repo.get_status(other_session, command_id)
        assert cross is None  # indistinguishable from not found
        missing = await repo.get_status(session_id, uuid4())
        assert missing is None
    finally:
        await _cleanup(session_id)
        await _cleanup(other_session)


# ---------------------------------------------------------------------------
# claiming / lease / fencing
# ---------------------------------------------------------------------------


async def test_claim_skip_locked_second_worker_gets_nothing() -> None:
    session_id = await _seed_session()
    try:
        command_id = await _enqueue(session_id, key="kc", payload={})
        repo_a = PostgresAsyncCommandRepository(get_session_factory())
        repo_b = PostgresAsyncCommandRepository(get_session_factory())
        claimed_a, claimed_b = await asyncio.gather(
            repo_a.claim(worker_id="worker-a", limit=10, lease_seconds=60, max_attempts=8),
            repo_b.claim(worker_id="worker-b", limit=10, lease_seconds=60, max_attempts=8),
        )
        assert len(claimed_a) + len(claimed_b) == 1
        # Exactly one of the two concurrent workers wins the claim.
        winner = (claimed_a or claimed_b)[0]
        assert winner.command_id == command_id
        row = await _command_row(command_id)
        assert row is not None
        assert row.status == STATUS_RUNNING
        assert row.attempt_count == 1
        assert row.lease_token is not None
    finally:
        await _cleanup(session_id)


async def test_claim_reclaims_expired_lease_and_increments_attempt() -> None:
    session_id = await _seed_session()
    try:
        command_id = await _enqueue(session_id, key="ke", payload={})
        repo = PostgresAsyncCommandRepository(get_session_factory())
        first = await repo.claim(worker_id="worker-a", limit=10, lease_seconds=60, max_attempts=8)
        assert first[0].attempt_count == 1
        # Expire the lease as if the worker crashed.
        async with get_session_factory()() as db, db.begin():
            await db.execute(
                update(AsyncCommand)
                .where(AsyncCommand.id == command_id)
                .values(lease_expires_at=func.now() - timedelta(seconds=1))
            )
        second = await repo.claim(worker_id="worker-b", limit=10, lease_seconds=60, max_attempts=8)
        assert len(second) == 1
        assert second[0].command_id == command_id
        assert second[0].attempt_count == 2
        assert second[0].lease_token != first[0].lease_token
    finally:
        await _cleanup(session_id)


async def test_renew_lease_only_under_owner_token() -> None:
    session_id = await _seed_session()
    try:
        command_id = await _enqueue(session_id, key="kr", payload={})
        repo = PostgresAsyncCommandRepository(get_session_factory())
        claimed = await repo.claim(worker_id="worker-a", limit=10, lease_seconds=60, max_attempts=8)
        token = claimed[0].lease_token
        assert await repo.renew_lease(command_id, worker_id="worker-a", lease_token=token, lease_seconds=60) is True
        assert await repo.renew_lease(command_id, worker_id="worker-a", lease_token=uuid4(), lease_seconds=60) is False
        assert await repo.renew_lease(command_id, worker_id="intruder", lease_token=token, lease_seconds=60) is False
    finally:
        await _cleanup(session_id)


async def test_stale_owner_cannot_settle_after_takeover() -> None:
    session_id = await _seed_session()
    try:
        command_id = await _enqueue(session_id, key="kf", payload={})
        repo_a = PostgresAsyncCommandRepository(get_session_factory())
        repo_b = PostgresAsyncCommandRepository(get_session_factory())
        first = await repo_a.claim(worker_id="worker-a", limit=10, lease_seconds=60, max_attempts=8)
        async with get_session_factory()() as db, db.begin():
            await db.execute(
                update(AsyncCommand)
                .where(AsyncCommand.id == command_id)
                .values(lease_expires_at=func.now() - timedelta(seconds=1))
            )
        second = await repo_b.claim(worker_id="worker-b", limit=10, lease_seconds=60, max_attempts=8)
        assert second[0].attempt_count == 2

        # worker-a (stale) tries to complete with its old token -> refused.
        stale_ok = await repo_a.complete(
            command_id,
            worker_id="worker-a",
            lease_token=first[0].lease_token,
            http_status=200,
            result_payload={},
        )
        assert stale_ok is False
        # worker-b (current owner) completes -> accepted.
        assert (
            await repo_b.complete(
                command_id,
                worker_id="worker-b",
                lease_token=second[0].lease_token,
                http_status=200,
                result_payload={},
            )
            is True
        )
        row = await _command_row(command_id)
        assert row is not None and row.status == STATUS_SUCCEEDED
        # Exactly one succeeded outbox row was written.
        succeeded = [r for r in await _outbox_rows(session_id) if r.event_type == "async_command.succeeded.v1"]
        assert len(succeeded) == 1
    finally:
        await _cleanup(session_id)


async def test_fail_and_retry_are_owner_fenced_and_terminal() -> None:
    session_id = await _seed_session()
    try:
        command_id = await _enqueue(session_id, key="kg", payload={})
        repo = PostgresAsyncCommandRepository(get_session_factory())
        claimed = await repo.claim(worker_id="worker-a", limit=10, lease_seconds=60, max_attempts=8)

        # retry requires owner; a wrong token is refused.
        assert await repo.retry(command_id, worker_id="intruder", lease_token=uuid4(), retry_after_seconds=0) is False
        assert (
            await repo.retry(
                command_id, worker_id="worker-a", lease_token=claimed[0].lease_token, retry_after_seconds=0
            )
            is True
        )
        row = await _command_row(command_id)
        assert row is not None and row.status == "queued"
        assert row.lease_owner is None and row.lease_token is None
        # Retry is an externally meaningful running -> queued transition: it
        # writes a queued.v1 outbox row in the same transaction (the original
        # enqueue queued.v1 plus this retry queued.v1).
        queued = [r for r in await _outbox_rows(session_id) if r.event_type == "async_command.queued.v1"]
        assert len(queued) == 2
        assert queued[1].payload["status"] == "queued"
        assert queued[1].payload["attempt"] == row.attempt_count

        claimed2 = await repo.claim(worker_id="worker-a", limit=10, lease_seconds=60, max_attempts=8)
        assert (
            await repo.fail(
                command_id,
                worker_id="worker-a",
                lease_token=claimed2[0].lease_token,
                error_code="HANDLER_REJECTED",
                error_payload={"why": "x"},
            )
            is True
        )
        row = await _command_row(command_id)
        assert row is not None and row.status == STATUS_FAILED
        assert row.error_code == "HANDLER_REJECTED"
        # R6-A error_payload is a sanitized empty object, even when the caller
        # passes a private {"why": "x"} payload.
        assert row.error_payload == {}
        failed = [r for r in await _outbox_rows(session_id) if r.event_type == "async_command.failed.v1"]
        assert len(failed) == 1
        assert failed[0].payload["error_code"] == "HANDLER_REJECTED"
    finally:
        await _cleanup(session_id)


# ---------------------------------------------------------------------------
# worker end-to-end
# ---------------------------------------------------------------------------


async def test_worker_success_and_same_transaction_outbox() -> None:
    session_id = await _seed_session()
    try:
        command_id = await _enqueue(session_id, key="kw", payload={"a": 1})

        async def handler(ctx: AsyncCommandContext) -> CommandSuccess:
            return CommandSuccess(http_status=200, result_payload={"ok": True})

        worker = AsyncCommandWorker(
            PostgresAsyncCommandRepository(get_session_factory()),
            handlers={OPERATION: handler},
            worker_id="worker-int",
        )
        result = await worker.run_once()
        assert result.succeeded == 1

        row = await _command_row(command_id)
        assert row is not None and row.status == STATUS_SUCCEEDED
        assert row.attempt_count == 1
        events = [r.event_type for r in await _outbox_rows(session_id)]
        assert events == [
            "async_command.queued.v1",
            "async_command.running.v1",
            "async_command.succeeded.v1",
        ]
    finally:
        await _cleanup(session_id)


async def test_worker_unknown_operation_rejects_terminal() -> None:
    session_id = await _seed_session()
    try:
        command_id = await _enqueue(session_id, key="kx1", payload={})
        await _command_row(command_id)
        worker = AsyncCommandWorker(
            PostgresAsyncCommandRepository(get_session_factory()),
            handlers={},  # no handler for this operation
            worker_id="worker-unknown",
        )
        result = await worker.run_once()
        assert result.rejected == 1
        row = await _command_row(command_id)
        assert row is not None and row.status == STATUS_FAILED
        assert row.error_code == "UNKNOWN_OPERATION"
    finally:
        await _cleanup(session_id)


async def test_worker_retries_transient_then_succeeds() -> None:
    session_id = await _seed_session()
    try:
        command_id = await _enqueue(session_id, key="ky", payload={})
        calls = 0

        async def handler(ctx: AsyncCommandContext) -> CommandSuccess:
            nonlocal calls
            calls += 1
            if calls < 2:
                raise CommandFailureError(error_code="UPSTREAM_TIMEOUT", retryable=True)
            return CommandSuccess(http_status=200, result_payload={"ok": True})

        worker = AsyncCommandWorker(
            PostgresAsyncCommandRepository(get_session_factory()),
            handlers={OPERATION: handler},
            worker_id="worker-retry",
            retry_base_seconds=0,
            retry_max_seconds=0,
        )
        first = await worker.run_once()
        assert first.retried == 1
        row = await _command_row(command_id)
        assert row is not None and row.status == "queued" and row.attempt_count == 1
        second = await worker.run_once()
        assert second.succeeded == 1
        row = await _command_row(command_id)
        assert row is not None and row.status == STATUS_SUCCEEDED and row.attempt_count == 2
    finally:
        await _cleanup(session_id)


async def test_worker_permanent_failure_and_max_attempts_exhaustion() -> None:
    session_id = await _seed_session()
    try:
        command_id = await _enqueue(session_id, key="kz", payload={})
        repo = PostgresAsyncCommandRepository(get_session_factory())

        # permanent failure -> terminal failed on first claim. The dynamic code
        # PATIENT_BLOCKED is outside the allowlist and collapses to UNKNOWN.
        async def hard_fail(ctx: AsyncCommandContext) -> CommandSuccess:
            raise CommandFailureError(error_code="PATIENT_BLOCKED", retryable=False)

        worker = AsyncCommandWorker(repo, handlers={OPERATION: hard_fail}, worker_id="worker-fail")
        result = await worker.run_once()
        assert result.failed == 1
        row = await _command_row(command_id)
        assert row is not None and row.status == STATUS_FAILED
        assert row.error_code == "UNKNOWN"
        assert row.error_payload == {}

        # transient exhaustion: max_attempts=2 -> fails on the second claim
        cmd2 = await _enqueue(session_id, key="kz2", payload={})
        calls = 0

        async def flappy(ctx: AsyncCommandContext) -> CommandSuccess:
            nonlocal calls
            calls += 1
            raise CommandFailureError(error_code="FLAPPY", retryable=True)

        worker2 = AsyncCommandWorker(
            repo,
            handlers={OPERATION: flappy},
            worker_id="worker-flap",
            max_attempts=2,
            retry_base_seconds=0,
            retry_max_seconds=0,
        )
        assert (await worker2.run_once()).retried == 1
        assert (await worker2.run_once()).failed == 1
        row = await _command_row(cmd2)
        assert row is not None and row.status == STATUS_FAILED
        assert row.error_code == "UNKNOWN"  # FLAPPY collapsed to the fixed bucket
        assert row.error_payload == {}
        assert row.attempt_count == 2
    finally:
        await _cleanup(session_id)


async def test_worker_graceful_stop_drains_claimed_batch() -> None:
    session_id = await _seed_session()
    try:
        command_id = await _enqueue(session_id, key="kwg", payload={})
        release = asyncio.Event()
        started = asyncio.Event()

        async def slow(ctx: AsyncCommandContext) -> CommandSuccess:
            started.set()
            await release.wait()
            return CommandSuccess(http_status=200, result_payload={"ok": True})

        worker = AsyncCommandWorker(
            PostgresAsyncCommandRepository(get_session_factory()),
            handlers={OPERATION: slow},
            worker_id="worker-graceful",
        )
        stop = asyncio.Event()
        task = asyncio.create_task(worker.run_forever(stop))
        # Let the worker claim and enter the handler.
        await asyncio.wait_for(started.wait(), timeout=5)
        stop.set()
        # A claimed item is finished before the loop returns.
        release.set()
        await asyncio.wait_for(task, timeout=5)
        row = await _command_row(command_id)
        assert row is not None and row.status == STATUS_SUCCEEDED
    finally:
        await _cleanup(session_id)


async def test_worker_stale_owner_stops_at_next_guard_observation() -> None:
    """A stale worker cannot continue or settle after a real takeover.

    Worker A claims a command and its handler blocks (a clinical side effect in
    progress). Another worker B expires the lease, reclaims it and completes.
    Worker A's next guard renewal observes the lost ownership, cancels the
    blocked handler and returns ``ownership_lost`` without settling — the stale
    handler's side effect never runs and worker B remains authoritative.
    """
    session_id = await _seed_session()
    try:
        command_id = await _enqueue(session_id, key="k-r8b", payload={})
        repo = PostgresAsyncCommandRepository(get_session_factory())
        started = asyncio.Event()
        stale_side_effect = asyncio.Event()

        async def blocked_handler(ctx: AsyncCommandContext) -> CommandSuccess:
            del ctx
            started.set()
            await asyncio.Event().wait()  # never set; only cancellation ends this
            stale_side_effect.set()  # reached only if the stale handler ran on
            return CommandSuccess(http_status=200, result_payload={})

        # Worker A holds the claim; its first renew is one second away, so the
        # takeover below happens well before worker A can re-extend.
        worker_a = AsyncCommandWorker(
            repo,
            handlers={OPERATION: blocked_handler},
            worker_id="worker-a-r8b",
            heartbeat_interval_seconds=1,
            lease_seconds=60,
        )
        task_a = asyncio.create_task(worker_a.run_once())
        await asyncio.wait_for(started.wait(), timeout=5)

        # Take over: expire the lease, worker B claims and completes.
        async with get_session_factory()() as db, db.begin():
            await db.execute(
                update(AsyncCommand)
                .where(AsyncCommand.id == command_id)
                .values(lease_expires_at=func.now() - timedelta(seconds=1))
            )
        worker_b = AsyncCommandWorker(
            PostgresAsyncCommandRepository(get_session_factory()),
            handlers={OPERATION: await _ok_handler()},
            worker_id="worker-b-r8b",
        )
        result_b = await worker_b.run_once()
        assert result_b.succeeded == 1

        # Worker A's next guard observation sees the loss, cancels the blocked
        # handler and reports ownership_lost without settling.
        result_a = await asyncio.wait_for(task_a, timeout=10)
        assert result_a.ownership_lost == 1
        assert result_a.succeeded == 0
        # The stale handler never reached its clinical side effect.
        assert not stale_side_effect.is_set()

        # Worker B (current owner) remains authoritative: the command is
        # succeeded with B's outcome and exactly one succeeded outbox row.
        row = await _command_row(command_id)
        assert row is not None
        assert row.status == STATUS_SUCCEEDED
        assert row.attempt_count == 2
        succeeded = [r for r in await _outbox_rows(session_id) if r.event_type == "async_command.succeeded.v1"]
        assert len(succeeded) == 1
    finally:
        await _cleanup(session_id)


async def test_worker_forced_cancel_is_recovered_by_lease_reclaim() -> None:
    session_id = await _seed_session()
    try:
        command_id = await _enqueue(session_id, key="kwc", payload={})
        release = asyncio.Event()
        started = asyncio.Event()

        async def stuck(ctx: AsyncCommandContext) -> CommandSuccess:
            started.set()
            await release.wait()
            return CommandSuccess(http_status=200, result_payload={})

        worker = AsyncCommandWorker(
            PostgresAsyncCommandRepository(get_session_factory()),
            handlers={OPERATION: stuck},
            worker_id="worker-cancel",
        )
        task = asyncio.create_task(worker.run_once())
        await asyncio.wait_for(started.wait(), timeout=5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        # The command is left running/leased; expire and let another worker finish.
        async with get_session_factory()() as db, db.begin():
            await db.execute(
                update(AsyncCommand)
                .where(AsyncCommand.id == command_id)
                .values(lease_expires_at=func.now() - timedelta(seconds=1))
            )
        release.set()
        recovery = AsyncCommandWorker(
            PostgresAsyncCommandRepository(get_session_factory()),
            handlers={OPERATION: await _ok_handler()},
            worker_id="worker-recover",
        )
        await recovery.run_once()
        row = await _command_row(command_id)
        assert row is not None and row.status == STATUS_SUCCEEDED
        assert row.attempt_count == 2
    finally:
        await _cleanup(session_id)


# ---------------------------------------------------------------------------
# Outbox publication + Redis append-once + privacy
# ---------------------------------------------------------------------------


async def test_publisher_maps_command_lifecycle_and_dedupes_on_redis() -> None:
    session_id = await _seed_session()
    try:
        await _enqueue(session_id, key="kp", payload={"private": "PHI-secret"})
        publisher = OutboxPublisher(
            PostgresDomainRepository(get_session_factory()),
            EventService(dedupe_ttl_seconds=30),
            worker_id="worker-publisher",
        )
        result = await publisher.run_once()
        assert result.published >= 1

        redis = await get_redis()
        stream_key = session_event_stream_key(str(session_id))
        rows = await redis.xrange(stream_key)
        assert [row[1]["event_type"] for row in rows] == ["command.queued"]
        payload = json.loads(rows[0][1]["payload"])
        assert payload["command_id"]
        assert payload["operation"] == OPERATION
        assert payload["status"] == "queued"
        assert "attempt" in payload
        # PHI and request digests never reach the client stream.
        assert "PHI-secret" not in json.dumps(rows)
        assert "private" not in payload

        # Re-running the publisher must not duplicate our event: the row was
        # acknowledged and the stream stays a single command.queued entry.
        await publisher.run_once()
        assert await redis.xlen(stream_key) == 1
    finally:
        await _cleanup(session_id)


async def test_private_payload_never_leaks_to_outbox_redis_or_status() -> None:
    secret = "PHI-secret-9f8e7d"
    session_id = await _seed_session()
    try:
        command_id = await _enqueue(session_id, key="kpr", payload={"patient": secret})
        repo = PostgresAsyncCommandRepository(get_session_factory())
        claimed = await repo.claim(worker_id="worker-priv", limit=10, lease_seconds=60, max_attempts=8)
        assert claimed[0].command_id == command_id
        await repo.complete(
            command_id,
            worker_id="worker-priv",
            lease_token=claimed[0].lease_token,
            http_status=200,
            result_payload={},
        )

        # Outbox rows never carry the private payload or digests.
        for row in await _outbox_rows(session_id):
            assert secret not in json.dumps(row.payload)
            assert secret not in row.trace_id

        # Status projection never carries it.
        status = await repo.get_status(session_id, command_id)
        assert status is not None
        assert secret not in json.dumps(status.model_dump(mode="json"))
    finally:
        await _cleanup(session_id)


async def test_status_api_all_states_and_cross_session() -> None:
    session_id = await _seed_session()
    other_session = await _seed_session()
    # Use an in-process ASGI client on the same (session-scoped) event loop as
    # the rest of the test, closed cleanly on exit. A synchronous TestClient
    # here would cross event-loop boundaries and trip a Windows access violation
    # after many passes.
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        try:
            command_id = await _enqueue(session_id, key="kapi", payload={})
            repo = PostgresAsyncCommandRepository(get_session_factory())
            url = f"/api/v1/consult/sessions/{session_id}/commands/{command_id}"

            queued = await client.get(url)
            assert queued.status_code == 200  # status query, not 202
            assert queued.json()["data"]["status"] == "queued"

            claimed = await repo.claim(worker_id="worker-api", limit=10, lease_seconds=60, max_attempts=8)
            running = await client.get(url)
            assert running.status_code == 200
            assert running.json()["data"]["status"] == "running"

            # fail it terminally with the token from the (still-held) claim and
            # check the error envelope. Only the fixed error code is public; the
            # private error_payload (why: x) is never projected.
            await repo.fail(
                command_id,
                worker_id="worker-api",
                lease_token=claimed[0].lease_token,
                error_code="HANDLER_REJECTED",
                error_payload={"why": "x"},
            )
            failed = await client.get(url)
            assert failed.status_code == 200
            data = failed.json()["data"]
            assert data["status"] == "failed"
            assert data["error"]["code"] == "HANDLER_REJECTED"
            assert data["result"] is None
            # The public body exposes no payload field at all.
            assert "payload" not in data["error"]
            assert "payload" not in (data["result"] or {})

            # cross-session and missing command are indistinguishable 404s
            cross = await client.get(f"/api/v1/consult/sessions/{other_session}/commands/{command_id}")
            missing = await client.get(f"/api/v1/consult/sessions/{session_id}/commands/{uuid4()}")
            assert cross.status_code == 404
            assert missing.status_code == 404
            assert cross.json()["code"] == missing.json()["code"] == "COMMAND_NOT_FOUND"

            # A missing session uses the SessionNotFoundError envelope, distinct
            # from a missing command.
            no_session = await client.get(f"/api/v1/consult/sessions/{uuid4()}/commands/{command_id}")
            assert no_session.status_code == 404
            assert no_session.json()["code"] == "SESSION_NOT_FOUND"
        finally:
            await _cleanup(session_id)
            await _cleanup(other_session)


# ---------------------------------------------------------------------------
# concurrency races (real independent repository sessions/connections)
# ---------------------------------------------------------------------------


async def test_concurrent_same_key_enqueue_resolves_to_exact_replay() -> None:
    """Concurrent same-session/same-key enqueues replay the SAME logical command.

    The loser re-reads the logical key after the unique-race rollback and
    returns a replay — it never surfaces an internal _EnqueueRetry/IntegrityError.
    """
    session_id = await _seed_session()
    try:
        repo_a = PostgresAsyncCommandRepository(get_session_factory())
        repo_b = PostgresAsyncCommandRepository(get_session_factory())
        refs = await asyncio.gather(
            repo_a.enqueue(
                session_id=session_id,
                operation=OPERATION,
                idempotency_key="race-same",
                request_payload={"a": 1},
            ),
            repo_b.enqueue(
                session_id=session_id,
                operation=OPERATION,
                idempotency_key="race-same",
                request_payload={"a": 1},
            ),
        )
        # Exactly one fresh insert + one deterministic replay, same command.
        assert refs[0].command_id == refs[1].command_id
        assert sum(1 for r in refs if r.replayed) == 1
        assert sum(1 for r in refs if not r.replayed) == 1
        # Only one command row and one queued outbox row.
        row = await _command_row(refs[0].command_id)
        assert row is not None
        queued = [r for r in await _outbox_rows(session_id) if r.event_type == "async_command.queued.v1"]
        assert len(queued) == 1
    finally:
        await _cleanup(session_id)


async def test_concurrent_different_key_enqueue_resolves_to_session_busy() -> None:
    """Concurrent same-session/different-key enqueues: exactly one wins the slot.

    The loser exhausts the bounded retry and raises SessionBusyError — never an
    internal _EnqueueRetry/IntegrityError leaking to the caller.
    """
    session_id = await _seed_session()
    try:
        repo_a = PostgresAsyncCommandRepository(get_session_factory())
        repo_b = PostgresAsyncCommandRepository(get_session_factory())
        results = await asyncio.gather(
            repo_a.enqueue(
                session_id=session_id,
                operation=OPERATION,
                idempotency_key="race-ka",
                request_payload={"a": 1},
            ),
            repo_b.enqueue(
                session_id=session_id,
                operation=OPERATION,
                idempotency_key="race-kb",
                request_payload={"b": 2},
            ),
            return_exceptions=True,
        )
        ok = [r for r in results if not isinstance(r, Exception)]
        errs = [r for r in results if isinstance(r, Exception)]
        assert len(ok) == 1
        assert len(errs) == 1
        assert isinstance(errs[0], SessionBusyError)
        # Exactly one command row (the winner) and one queued outbox.
        async with get_session_factory()() as db:
            rows = list((await db.scalars(select(AsyncCommand).where(AsyncCommand.session_id == session_id))).all())
        assert len(rows) == 1
        queued = [r for r in await _outbox_rows(session_id) if r.event_type == "async_command.queued.v1"]
        assert len(queued) == 1
    finally:
        await _cleanup(session_id)


# ---------------------------------------------------------------------------
# max attempts enforced on crash / lease-expiry reclaim (not only handler failure)
# ---------------------------------------------------------------------------


async def test_claim_terminal_fails_exhausted_crash_without_reclaim() -> None:
    session_id = await _seed_session()
    try:
        command_id = await _enqueue(session_id, key="kxh", payload={})
        repo = PostgresAsyncCommandRepository(get_session_factory())
        max_attempts = 2

        async def _expire_lease() -> None:
            async with get_session_factory()() as db, db.begin():
                await db.execute(
                    update(AsyncCommand)
                    .where(AsyncCommand.id == command_id)
                    .values(lease_expires_at=func.now() - timedelta(seconds=1))
                )

        # Claim 1 -> attempt 1. Simulate a crash by expiring the lease.
        assert (await repo.claim(worker_id="worker-a", limit=10, lease_seconds=60, max_attempts=max_attempts))[
            0
        ].attempt_count == 1
        await _expire_lease()

        # Claim 2 (reclaim after crash) -> attempt 2 == max_attempts.
        assert (await repo.claim(worker_id="worker-b", limit=10, lease_seconds=60, max_attempts=max_attempts))[
            0
        ].attempt_count == 2
        await _expire_lease()

        # Claim 3: the budget is already exhausted. It must be terminal-failed
        # atomically with ATTEMPTS_EXHAUSTED, never dispatched again.
        claimed = await repo.claim(worker_id="worker-c", limit=10, lease_seconds=60, max_attempts=max_attempts)
        assert len(claimed) == 0
        row = await _command_row(command_id)
        assert row is not None
        assert row.status == STATUS_FAILED
        assert row.error_code == "ATTEMPTS_EXHAUSTED"
        assert row.error_payload == {}
        assert row.attempt_count == max_attempts
        # Owner fencing preserved: lease cleared, not held by worker-c.
        assert row.lease_owner is None and row.lease_token is None
        failed = [r for r in await _outbox_rows(session_id) if r.event_type == "async_command.failed.v1"]
        assert len(failed) == 1
        assert failed[0].payload["error_code"] == "ATTEMPTS_EXHAUSTED"
    finally:
        await _cleanup(session_id)


async def test_worker_exhausted_crash_is_terminal_not_dispatched() -> None:
    """A worker reclaiming an exhausted crashed command terminal-fails it."""
    session_id = await _seed_session()
    try:
        command_id = await _enqueue(session_id, key="kwxh", payload={})
        repo = PostgresAsyncCommandRepository(get_session_factory())
        # Claim twice, crashing in between, with max_attempts=2.
        await repo.claim(worker_id="worker-a", limit=10, lease_seconds=60, max_attempts=2)
        async with get_session_factory()() as db, db.begin():
            await db.execute(
                update(AsyncCommand)
                .where(AsyncCommand.id == command_id)
                .values(lease_expires_at=func.now() - timedelta(seconds=1))
            )
        await repo.claim(worker_id="worker-b", limit=10, lease_seconds=60, max_attempts=2)
        async with get_session_factory()() as db, db.begin():
            await db.execute(
                update(AsyncCommand)
                .where(AsyncCommand.id == command_id)
                .values(lease_expires_at=func.now() - timedelta(seconds=1))
            )

        executed = False

        async def handler(ctx: AsyncCommandContext) -> CommandSuccess:
            nonlocal executed
            executed = True
            return CommandSuccess(http_status=200, result_payload={})

        worker = AsyncCommandWorker(repo, handlers={OPERATION: handler}, worker_id="worker-xh", max_attempts=2)
        result = await worker.run_once()
        assert result.claimed == 0  # nothing to dispatch
        assert executed is False
        row = await _command_row(command_id)
        assert row is not None and row.status == STATUS_FAILED
        assert row.error_code == "ATTEMPTS_EXHAUSTED"
    finally:
        await _cleanup(session_id)


# ---------------------------------------------------------------------------
# adversarial privacy: malicious nested PHI never reaches public surfaces
# ---------------------------------------------------------------------------


async def test_malicious_nested_phi_never_leaks_to_public_surfaces() -> None:
    secret = "PHI-nested-9f8e7d"
    session_id = await _seed_session()
    try:
        command_id = await _enqueue(session_id, key="kphi", payload={})
        repo = PostgresAsyncCommandRepository(get_session_factory())
        claimed = await repo.claim(worker_id="worker-phi", limit=10, lease_seconds=60, max_attempts=8)
        token = claimed[0].lease_token

        # A malicious producer attempts to seed deeply-nested PHI directly
        # through the repository fail path (bypassing the public enqueue/worker
        # path). The repository is a production boundary: regardless of input, it
        # persists exactly {} for error_payload.
        assert (
            await repo.fail(
                command_id,
                worker_id="worker-phi",
                lease_token=token,
                error_code="HANDLER_REJECTED",
                error_payload={"nested": {"deep": {"patient": secret}}},
            )
            is True
        )

        # The DB column holds only the sanitized empty object — the malicious
        # nested payload is discarded at the repository boundary.
        row = await _command_row(command_id)
        assert row.error_payload == {}
        assert secret not in json.dumps(row.error_payload)

        # ...but it never leaks to the public status projection.
        status = await repo.get_status(session_id, command_id)
        assert status is not None
        assert secret not in json.dumps(status.model_dump(mode="json"))
        assert secret not in repr(status)

        # ...never to Outbox rows.
        for outbox in await _outbox_rows(session_id):
            assert secret not in json.dumps(outbox.payload)
            assert secret not in outbox.trace_id

        # ...never to the Redis/SSE stream; only the fixed error code is emitted.
        publisher = OutboxPublisher(
            PostgresDomainRepository(get_session_factory()),
            EventService(dedupe_ttl_seconds=30),
            worker_id="worker-phi-pub",
        )
        await publisher.run_once()
        redis = await get_redis()
        rows = await redis.xrange(session_event_stream_key(str(session_id)))
        assert secret not in json.dumps(rows)
        failed_events = [r for r in rows if r[1]["event_type"] == "command.failed"]
        assert failed_events, "expected a command.failed event"
        for r in failed_events:
            payload = json.loads(r[1]["payload"])
            assert payload["error_code"] == "HANDLER_REJECTED"
            assert "payload" not in payload
            assert "nested" not in payload
    finally:
        await _cleanup(session_id)
