"""R7 async admission end-to-end integration tests (real PG/Redis).

Exercises the HTTP 202 path against real services using a *controlled fake
handler* (the spec's "controlled fake model/runtime") so the full pipeline is
exercised without invoking the real LangGraph graph:

- R7 default: when the substrate is enabled/ready/registered, admission returns
  202 *even without any ``Prefer: respond-async`` header* and enqueues a durable
  command + queued Outbox row, doing no inline work.
- When the feature is disabled/not ready, the request falls through to the
  synchronous R1-R5 path and NO command is enqueued (sync fallback preserved).
- A worker dispatch settles the command through real PG/OutboxPublisher/Redis,
  and the result stays private (never projected into status).
- Deterministic replay on the same public idempotency key returns the SAME
  command (202 with replayed=true); a concurrent different-key POST on an active
  session raises the deterministic SESSION_BUSY.
"""

from __future__ import annotations

import json
import uuid
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import delete, select

from app.agent_runtime.async_command import PostgresAsyncCommandRepository
from app.agent_runtime.async_command_admission import AsyncCommandAdmissionState
from app.agent_runtime.async_command_worker import (
    AsyncCommandContext,
    AsyncCommandWorker,
    CommandSuccess,
)
from app.agent_runtime.repository import PostgresDomainRepository
from app.core.redis import get_redis
from app.db.session import get_session_factory
from app.main import app
from app.models.async_command import AsyncCommand
from app.models.consult import ConsultSession
from app.models.domain import OutboxEvent
from app.services.events import (
    EventService,
    session_event_stream_key,
)
from app.services.outbox_publisher import OutboxPublisher

pytestmark = [pytest.mark.integration, pytest.mark.asyncio(loop_scope="session")]

OPERATIONS = ["intake.message", "session.advance", "prescription.review"]
SECRET = "PHI-admission-7f3e"


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


async def _cleanup(session_id: UUID) -> None:
    factory = get_session_factory()
    async with factory() as db, db.begin():
        await db.execute(delete(ConsultSession).where(ConsultSession.id == session_id))
    redis = await get_redis()
    await redis.delete(session_event_stream_key(str(session_id)))


def _set_admission_ready() -> None:
    app.state.async_command_state = AsyncCommandAdmissionState.ready_state(
        frozenset(OPERATIONS)
    )


def _clear_admission() -> None:
    if hasattr(app.state, "async_command_state"):
        delattr(app.state, "async_command_state")


async def _command_row(command_id: UUID):
    async with get_session_factory()() as db:
        return await db.get(AsyncCommand, command_id)


async def _outbox_events(session_id: UUID) -> list[OutboxEvent]:
    async with get_session_factory()() as db:
        return list(
            (
                await db.scalars(
                    select(OutboxEvent)
                    .where(OutboxEvent.session_id == session_id)
                    .order_by(OutboxEvent.created_at)
                )
            ).all()
        )


async def _fake_success_handler(ctx: AsyncCommandContext) -> CommandSuccess:
    del ctx
    return CommandSuccess(http_status=200, result_payload={"patient": SECRET})


async def _run_worker(session_id: UUID, *, max_attempts: int = 2) -> AsyncCommandWorker:
    worker = AsyncCommandWorker(
        PostgresAsyncCommandRepository(get_session_factory()),
        handlers={op: _fake_success_handler for op in OPERATIONS},
        worker_id=f"worker-adm-{uuid.uuid4().hex[:6]}",
        max_attempts=max_attempts,
        retry_base_seconds=0,
        retry_max_seconds=0,
    )
    await worker.run_once()
    return worker


def _post_url(session_id: UUID, operation: str) -> str:
    path = {
        "intake.message": f"/api/v1/consult/sessions/{session_id}/messages",
        "session.advance": f"/api/v1/consult/sessions/{session_id}/advance",
        "prescription.review": f"/api/v1/consult/sessions/{session_id}/review",
    }[operation]
    return path


def _body_for(operation: str) -> dict[str, Any]:
    if operation == "intake.message":
        return {"content": "医生问诊信息", "role": "doctor"}
    if operation == "session.advance":
        return {"force": False}
    return {"action": "confirm"}


# ---------------------------------------------------------------------------
# admission (202) per operation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("operation", OPERATIONS)
async def test_prefer_async_returns_202_for_each_operation(operation: str) -> None:
    session_id = await _seed_session()
    _set_admission_ready()
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.post(
                _post_url(session_id, operation),
                json=_body_for(operation),
                headers={"Prefer": "respond-async", "X-Idempotency-Key": f"k-{operation}"},
            )
            assert response.status_code == 202, response.text
            assert response.headers["Preference-Applied"] == "respond-async"
            assert response.headers["Retry-After"] == "1"
            data = response.json()["data"]
            assert data["operation"] == operation
            assert data["status"] == "queued"
            assert data["replayed"] is False
            assert data["attempt_count"] == 0
            command_id = data["command_id"]
            assert response.headers["Location"] == (
                f"/api/v1/consult/sessions/{session_id}/commands/{command_id}"
            )
            assert data["links"]["self"] == response.headers["Location"]
            assert data["links"]["session"] == f"/api/v1/consult/sessions/{session_id}"
            assert data["links"]["stream"] == f"/api/v1/consult/sessions/{session_id}/stream"

            # Durable command + queued outbox row; no inline execution yet.
            row = await _command_row(UUID(command_id))
            assert row is not None and row.status == "queued"
            queued = [
                e for e in await _outbox_events(session_id) if e.event_type == "async_command.queued.v1"
            ]
            assert len(queued) == 1
            # The private request payload never reaches the public body.
            assert SECRET not in response.text
    finally:
        _clear_admission()
        await _cleanup(session_id)


async def test_202_accepts_fast_without_inline_work() -> None:
    session_id = await _seed_session()
    _set_admission_ready()
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.post(
                _post_url(session_id, "intake.message"),
                json=_body_for("intake.message"),
                headers={"Prefer": "respond-async", "X-Idempotency-Key": "k-fast"},
            )
            assert response.status_code == 202
            # The message handler is the only producer of message rows; at
            # admission time nothing has run, so the command is still queued.
            row = await _command_row(UUID(response.json()["data"]["command_id"]))
            assert row is not None and row.status == "queued"
    finally:
        _clear_admission()
        await _cleanup(session_id)


async def test_admission_rejects_when_queue_overloaded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """阶段4 背压：未终结命令数达到阈值时 admission 返回 503，不再堆积。"""
    from app.core.config import get_settings

    monkeypatch.setenv("ASYNC_COMMAND_MAX_QUEUE_DEPTH", "2")
    get_settings.cache_clear()
    try:
        _set_admission_ready()
        # 3 条 active 命令（不同 session 各 1 条），超过阈值 2。
        backlog_sessions: list[UUID] = []
        repo = PostgresAsyncCommandRepository(get_session_factory())
        for i in range(3):
            sid = await _seed_session()
            backlog_sessions.append(sid)
            await repo.enqueue(
                session_id=sid,
                operation="intake.message",
                idempotency_key=f"backlog-{i}",
                request_payload={},
            )

        target_session = await _seed_session()
        try:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client:
                response = await client.post(
                    _post_url(target_session, "intake.message"),
                    json=_body_for("intake.message"),
                    headers={"Prefer": "respond-async", "X-Idempotency-Key": "k-overload"},
                )
                assert response.status_code == 503, response.text
                body = response.json()
                assert body["code"] == "QUEUE_OVERLOADED"
                assert body["retryable"] is True
                assert body["detail"] is None
                assert response.headers["Retry-After"] == "1"
        finally:
            await _cleanup(target_session)
        for sid in backlog_sessions:
            await _cleanup(sid)
    finally:
        _clear_admission()
        get_settings.cache_clear()


# ---------------------------------------------------------------------------
# R7 default: ready => 202 even without any Prefer header
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("operation", OPERATIONS)
async def test_ready_defaults_to_202_without_header(operation: str) -> None:
    """R7: when the substrate is ready, the async 202 path is the default.

    No ``Prefer: respond-async`` header is required. Admission still commits a
    durable command + queued Outbox row and does no inline execution.
    """
    session_id = await _seed_session()
    _set_admission_ready()
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.post(
                _post_url(session_id, operation),
                json=_body_for(operation),
                headers={"X-Idempotency-Key": f"k-r7-{operation}"},
            )
            assert response.status_code == 202, response.text
            # R7 default admission returns 202 without any Prefer header and must
            # not claim a preference the client never sent.
            assert "Preference-Applied" not in response.headers
            assert response.headers["Location"].endswith(f"/commands/{response.json()['data']['command_id']}")
            assert response.headers["Retry-After"] == "1"
            data = response.json()["data"]
            assert data["operation"] == operation
            assert data["status"] == "queued"
            # No inline execution: the command is still queued at admission time.
            row = await _command_row(UUID(data["command_id"]))
            assert row is not None and row.status == "queued"
            queued = [
                e
                for e in await _outbox_events(session_id)
                if e.event_type == "async_command.queued.v1"
            ]
            assert len(queued) == 1
    finally:
        _clear_admission()
        await _cleanup(session_id)


# ---------------------------------------------------------------------------
# sync fallback preserved (feature disabled / not ready / partial registry)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("operation", OPERATIONS)
async def test_not_ready_falls_back_to_sync_no_command_row(operation: str) -> None:
    """When the substrate is unavailable, the request runs the existing sync
    path and never enqueues a command (byte/field/error semantics unchanged)."""
    session_id = await _seed_session()
    _clear_admission()  # feature disabled => admission state absent
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.post(
                _post_url(session_id, operation),
                json=_body_for(operation),
                headers={"X-Idempotency-Key": f"k-sync-{operation}"},
            )
            # The request runs the existing sync path (here it fails fast because
            # no real runtime is running) and never enqueues a command — the
            # response is definitely not a 202.
            assert response.status_code != 202
            assert "Preference-Applied" not in response.headers
            async with get_session_factory()() as db:
                rows = list(
                    (
                        await db.scalars(
                            select(AsyncCommand).where(AsyncCommand.session_id == session_id)
                        )
                    ).all()
                )
            assert rows == []
    finally:
        await _cleanup(session_id)


@pytest.mark.parametrize("operation", OPERATIONS)
async def test_disabled_feature_ignores_preference(operation: str) -> None:
    """Never enqueue without a worker: disabled/not-ready => preference ignored."""
    session_id = await _seed_session()
    _clear_admission()  # feature disabled => admission state absent
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.post(
                _post_url(session_id, operation),
                json=_body_for(operation),
                headers={"Prefer": "respond-async", "X-Idempotency-Key": f"k-dis-{operation}"},
            )
            assert response.status_code != 202
            assert "Preference-Applied" not in response.headers
            async with get_session_factory()() as db:
                rows = list(
                    (
                        await db.scalars(
                            select(AsyncCommand).where(AsyncCommand.session_id == session_id)
                        )
                    ).all()
                )
            assert rows == []
    finally:
        await _cleanup(session_id)


# ---------------------------------------------------------------------------
# worker dispatch settles 202 -> redis, result stays private
# ---------------------------------------------------------------------------


async def test_worker_completes_accepted_command_to_status_outbox_redis() -> None:
    session_id = await _seed_session()
    _set_admission_ready()
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            accepted = await client.post(
                _post_url(session_id, "intake.message"),
                json=_body_for("intake.message"),
                headers={"Prefer": "respond-async", "X-Idempotency-Key": "k-work"},
            )
            assert accepted.status_code == 202
            command_id = accepted.json()["data"]["command_id"]

            await _run_worker(session_id)

            row = await _command_row(UUID(command_id))
            assert row is not None and row.status == "succeeded"
            assert row.attempt_count == 1

            # Status API exposes only the result HTTP status — never the private
            # result_payload (which holds SECRET).
            status = await client.get(
                f"/api/v1/consult/sessions/{session_id}/commands/{command_id}"
            )
            assert status.status_code == 200
            data = status.json()["data"]
            assert data["status"] == "succeeded"
            assert data["result"]["http_status"] == 200
            assert SECRET not in status.text
            assert "patient" not in json.dumps(data)

            # Outbox carries the bounded succeeded projection, never SECRET.
            succeeded = [
                e
                for e in await _outbox_events(session_id)
                if e.event_type == "async_command.succeeded.v1"
            ]
            assert len(succeeded) == 1
            assert SECRET not in json.dumps(succeeded[0].payload)

            # Redis/SSE stream has the bounded command.succeeded event.
            publisher = OutboxPublisher(
                PostgresDomainRepository(get_session_factory()),
                EventService(dedupe_ttl_seconds=30),
                worker_id=f"publisher-adm-{uuid.uuid4().hex[:6]}",
            )
            await publisher.run_once()
            redis = await get_redis()
            rows = await redis.xrange(session_event_stream_key(str(session_id)))
            event_types = [r[1]["event_type"] for r in rows]
            assert "command.queued" in event_types
            assert "command.running" in event_types
            assert "command.succeeded" in event_types
            assert SECRET not in json.dumps(rows)
    finally:
        _clear_admission()
        await _cleanup(session_id)


# ---------------------------------------------------------------------------
# deterministic replay + concurrency
# ---------------------------------------------------------------------------


async def test_same_key_202_replays_same_command() -> None:
    session_id = await _seed_session()
    _set_admission_ready()
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            headers = {"Prefer": "respond-async", "X-Idempotency-Key": "k-replay"}
            first = await client.post(_post_url(session_id, "intake.message"), json=_body_for("intake.message"), headers=headers)
            second = await client.post(_post_url(session_id, "intake.message"), json=_body_for("intake.message"), headers=headers)
            assert first.status_code == second.status_code == 202
            assert first.json()["data"]["command_id"] == second.json()["data"]["command_id"]
            assert first.json()["data"]["replayed"] is False
            assert second.json()["data"]["replayed"] is True
            async with get_session_factory()() as db:
                rows = list(
                    (
                        await db.scalars(
                            select(AsyncCommand).where(AsyncCommand.session_id == session_id)
                        )
                    ).all()
                )
            assert len(rows) == 1
            queued = [
                e for e in await _outbox_events(session_id) if e.event_type == "async_command.queued.v1"
            ]
            assert len(queued) == 1
    finally:
        _clear_admission()
        await _cleanup(session_id)


async def test_conflict_same_key_different_payload_returns_conflict() -> None:
    session_id = await _seed_session()
    _set_admission_ready()
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            headers = {"Prefer": "respond-async", "X-Idempotency-Key": "k-conflict"}
            body_a = _body_for("intake.message")
            body_b = {**_body_for("intake.message"), "content": "不同内容"}

            first = await client.post(
                _post_url(session_id, "intake.message"), json=body_a, headers=headers
            )
            assert first.status_code == 202
            second = await client.post(
                _post_url(session_id, "intake.message"), json=body_b, headers=headers
            )
            # Same key, different payload => deterministic conflict (not 202).
            assert second.status_code == 409
            assert second.json()["code"] == "IDEMPOTENCY_KEY_REUSED"
    finally:
        _clear_admission()
        await _cleanup(session_id)


async def test_concurrent_different_key_on_active_session_is_session_busy() -> None:
    session_id = await _seed_session()
    _set_admission_ready()
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            first = await client.post(
                _post_url(session_id, "intake.message"),
                json=_body_for("intake.message"),
                headers={"Prefer": "respond-async", "X-Idempotency-Key": "k-busy-a"},
            )
            assert first.status_code == 202
            second = await client.post(
                _post_url(session_id, "session.advance"),
                json=_body_for("session.advance"),
                headers={"Prefer": "respond-async", "X-Idempotency-Key": "k-busy-b"},
            )
            # An active (queued) command on the same session blocks the slot.
            assert second.status_code == 409
            assert second.json()["code"] == "SESSION_BUSY"
    finally:
        _clear_admission()
        await _cleanup(session_id)
