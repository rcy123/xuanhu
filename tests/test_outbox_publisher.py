from __future__ import annotations

import asyncio
from collections.abc import Sequence
from contextlib import asynccontextmanager
from typing import Any, cast
from uuid import UUID, uuid4

import pytest
from sqlalchemy import CheckConstraint, Table

from app.agent_runtime.repository import OutboxErrorCode, OutboxHealth, OutboxMessage
from app.core.config import get_settings
from app.main import app, lifespan
from app.models.domain import OutboxEvent
from app.schemas.events import SupportedEventType
from app.services.events import EventService
from app.services.outbox_publisher import (
    OutboxMappingError,
    OutboxPublisher,
    map_outbox_event,
)


def _message(
    event_type: str = "intake.message_created.v1",
    *,
    payload: dict[str, object] | None = None,
    attempt_count: int = 1,
) -> OutboxMessage:
    return OutboxMessage(
        event_id=uuid4(),
        event_type=event_type,
        session_id=uuid4(),
        graph_run_id=uuid4(),
        state_version=3,
        trace_id="trace:deadbeef",
        payload=payload or {"message_id": str(uuid4()), "role": "doctor", "stage": "inquiry"},
        status="leased",
        attempt_count=attempt_count,
        leased_by="worker-a",
    )


class FakeRepository:
    def __init__(self, batches: Sequence[tuple[OutboxMessage, ...]]) -> None:
        self.batches = list(batches)
        self.acks: list[UUID] = []
        self.releases: list[tuple[UUID, OutboxErrorCode, int]] = []
        self.dead_letters: list[tuple[UUID, OutboxErrorCode]] = []
        self.fail_ack_count = 0

    async def claim(self, *, worker_id: str, limit: int, lease_seconds: int) -> tuple[OutboxMessage, ...]:
        del worker_id, limit, lease_seconds
        return self.batches.pop(0) if self.batches else ()

    async def acknowledge(self, event_id: UUID, *, worker_id: str) -> bool:
        del worker_id
        if self.fail_ack_count:
            self.fail_ack_count -= 1
            raise RuntimeError("database unavailable")
        self.acks.append(event_id)
        return True

    async def release_failed(
        self,
        event_id: UUID,
        *,
        worker_id: str,
        error_code: OutboxErrorCode,
        retry_after_seconds: int,
    ) -> bool:
        del worker_id
        self.releases.append((event_id, error_code, retry_after_seconds))
        return True

    async def dead_letter(
        self,
        event_id: UUID,
        *,
        worker_id: str,
        error_code: OutboxErrorCode,
    ) -> bool:
        del worker_id
        self.dead_letters.append((event_id, error_code))
        return True

    async def get_outbox_health(self) -> OutboxHealth:
        return OutboxHealth(
            backlog_count=3,
            pending_count=2,
            leased_count=1,
            dead_letter_count=0,
            oldest_unpublished_age_seconds=4.5,
        )


class DedupeSink:
    def __init__(self) -> None:
        self.rows: list[tuple[str, SupportedEventType, dict[str, object], str]] = []
        self.seen: set[str] = set()
        self.calls = 0
        self.fail_on_calls: set[int] = set()
        self.failure: Exception = TimeoutError()

    async def append_session_event_once(
        self,
        session_id: str,
        event_type: SupportedEventType,
        payload: dict[str, object],
        *,
        dedupe_id: str,
    ) -> object:
        self.calls += 1
        if self.calls in self.fail_on_calls:
            raise self.failure
        if dedupe_id not in self.seen:
            self.seen.add(dedupe_id)
            self.rows.append((session_id, event_type, payload, dedupe_id))
        return object()


def test_versioned_mapper_projects_only_safe_fields() -> None:
    secret = "raw clinical complaint must never be emitted"
    message = _message(
        payload={
            "message_id": str(uuid4()),
            "role": "patient_proxy",
            "stage": "inquiry",
            "content": secret,
            "raw_prompt": secret,
        }
    )

    mapped = map_outbox_event(message)

    assert [item.event_type for item in mapped] == ["message.created", "agent.started"]
    assert secret not in str(mapped)
    assert set(mapped[0].payload) == {
        "source_event_id",
        "state_version",
        "message_id",
        "role",
        "stage",
    }


@pytest.mark.parametrize(
    ("event_type", "payload", "expected"),
    [
        (
            "intake.command_completed.v1",
            {
                "triage_decision": "passed",
                "question_message_id": str(uuid4()),
            },
            ["agent.finished", "message.created"],
        ),
        (
            "intake.command_completed.v1",
            {"triage_decision": "blocked"},
            ["agent.finished", "safety.blocked", "session.blocked"],
        ),
        (
            "intake.command_completed.v1",
            {"triage_decision": "passed", "completeness_disposition": "stagnated"},
            ["agent.finished", "session.blocked"],
        ),
        (
            "advance.command_started.v1",
            {"from_stage": "inquiry", "to_stage": "syndrome"},
            ["stage.changed", "agent.started"],
        ),
        (
            "reasoning.artifact_committed.v1",
            {"artifact_type": "syndrome_draft", "artifact_id": str(uuid4()), "revision": 1},
            ["agent.finished"],
        ),
        (
            "reasoning.command_completed.v1",
            {"route": "needs_more_info", "question_message_id": str(uuid4())},
            ["agent.finished", "stage.changed", "message.created"],
        ),
        (
            "reasoning.command_completed.v1",
            {"route": "manual_required"},
            ["agent.finished", "session.blocked"],
        ),
        (
            "reasoning.command_completed.v1",
            {"artifact_type": "formula_draft"},
            ["agent.finished", "stage.changed"],
        ),
        ("domain.state_committed.v1", {}, ["agent.finished"]),
    ],
)
def test_versioned_mapper_covers_current_internal_contracts(
    event_type: str,
    payload: dict[str, object],
    expected: list[str],
) -> None:
    assert [item.event_type for item in map_outbox_event(_message(event_type, payload=payload))] == expected


def test_versioned_mapper_rejects_unknown_contract() -> None:
    with pytest.raises(OutboxMappingError):
        map_outbox_event(_message("future.event.v2", payload={"content": "unsafe"}))


def test_outbox_orm_has_durable_dead_letter_state_and_index() -> None:
    table = cast(Table, OutboxEvent.__table__)
    assert "dead_lettered_at" in table.columns
    assert any(index.name == "idx_outbox_events_dead_lettered" for index in table.indexes)
    status_constraint = next(
        constraint
        for constraint in table.constraints
        if isinstance(constraint, CheckConstraint)
        and isinstance(constraint.name, str)
        and constraint.name.endswith("chk_outbox_events_status")
    )
    assert "dead_letter" in str(status_constraint.sqltext)


@pytest.mark.asyncio
async def test_run_once_publishes_then_acknowledges() -> None:
    message = _message()
    repository = FakeRepository([(message,)])
    sink = DedupeSink()
    publisher = OutboxPublisher(repository, sink, worker_id="worker-a")

    result = await publisher.run_once()

    assert result.claimed == result.published == 1
    assert repository.acks == [message.event_id]
    assert len(sink.rows) == 2


@pytest.mark.asyncio
async def test_transient_failure_releases_with_stable_code_and_exponential_backoff() -> None:
    message = _message(attempt_count=4)
    repository = FakeRepository([(message,)])
    sink = DedupeSink()
    sink.fail_on_calls = {1}
    publisher = OutboxPublisher(
        repository,
        sink,
        worker_id="worker-a",
        base_retry_seconds=2,
        max_retry_seconds=30,
    )

    result = await publisher.run_once()

    assert result.retried == 1
    assert repository.releases == [(message.event_id, OutboxErrorCode.PUBLISH_TIMEOUT, 16)]
    assert repository.acks == []


@pytest.mark.asyncio
async def test_max_attempts_moves_failure_to_durable_dlq() -> None:
    message = _message("unknown.event.v1", payload={}, attempt_count=3)
    repository = FakeRepository([(message,)])
    publisher = OutboxPublisher(repository, DedupeSink(), worker_id="worker-a", max_attempts=3)

    result = await publisher.run_once()

    assert result.dead_lettered == 1
    assert repository.dead_letters == [(message.event_id, OutboxErrorCode.PUBLISH_REJECTED)]
    assert repository.releases == []


@pytest.mark.asyncio
async def test_publish_then_ack_failure_replay_has_no_duplicate_client_side_effect() -> None:
    message = _message()
    repository = FakeRepository([(message,), (message,)])
    repository.fail_ack_count = 1
    sink = DedupeSink()
    publisher = OutboxPublisher(repository, sink, worker_id="worker-a")

    first = await publisher.run_once()
    second = await publisher.run_once()

    assert first.ownership_lost == 1
    assert second.published == 1
    assert repository.acks == [message.event_id]
    assert len(sink.rows) == 2
    assert sink.calls == 4


@pytest.mark.asyncio
async def test_partial_multi_event_failure_replays_only_missing_side_effect() -> None:
    message = _message(
        "advance.command_started.v1",
        payload={"from_stage": "inquiry", "to_stage": "syndrome"},
    )
    repository = FakeRepository([(message,), (message,)])
    sink = DedupeSink()
    sink.fail_on_calls = {2}
    publisher = OutboxPublisher(repository, sink, worker_id="worker-a", base_retry_seconds=0)

    first = await publisher.run_once()
    second = await publisher.run_once()

    assert first.retried == 1
    assert second.published == 1
    assert [item[1] for item in sink.rows] == ["stage.changed", "agent.started"]
    assert len(sink.rows) == 2


@pytest.mark.asyncio
async def test_run_forever_stops_promptly_while_idle_and_health_is_aggregate_only() -> None:
    repository = FakeRepository([])
    publisher = OutboxPublisher(repository, DedupeSink(), worker_id="worker-a", poll_interval_seconds=30)
    stop = asyncio.Event()
    task = asyncio.create_task(publisher.run_forever(stop))
    await asyncio.sleep(0)

    stop.set()
    await asyncio.wait_for(task, timeout=1)
    health = await publisher.health()

    assert health.pending_count == 2
    assert health.oldest_unpublished_age_seconds == 4.5


@pytest.mark.asyncio
async def test_application_lifespan_starts_and_gracefully_stops_configured_worker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started = asyncio.Event()
    stopped = asyncio.Event()

    class LifecyclePublisher:
        def __init__(self, *args: object, **kwargs: object) -> None:
            del args, kwargs

        async def run_forever(self, stop: asyncio.Event) -> None:
            started.set()
            await stop.wait()
            stopped.set()

    @asynccontextmanager
    async def fake_langgraph_runtime(_db_url: str):  # type: ignore[no-untyped-def]
        yield cast(Any, object())

    monkeypatch.setenv("OUTBOX_PUBLISHER_ENABLED", "true")
    monkeypatch.setattr("app.services.outbox_publisher.OutboxPublisher", LifecyclePublisher)
    monkeypatch.setattr("app.main.shared_langgraph_runtime", fake_langgraph_runtime)
    get_settings.cache_clear()
    try:
        async with lifespan(app):
            await asyncio.wait_for(started.wait(), timeout=1)
        await asyncio.wait_for(stopped.wait(), timeout=1)
    finally:
        get_settings.cache_clear()


class LuaFakeRedis:
    """Small behavioral fake for the append-once Redis script contract."""

    def __init__(self) -> None:
        self.streams: dict[str, list[tuple[str, dict[str, str]]]] = {}
        self.dedupe: dict[tuple[str, str], str] = {}
        self.dedupe_order: dict[str, list[str]] = {}
        self.ttls: dict[str, int] = {}
        self.expire_calls: dict[str, int] = {}

    async def eval(self, script: str, numkeys: int, *args: Any) -> list[object]:
        assert all(
            operation in script
            for operation in ("HGET", "XADD", "HSET", "RPUSH", "LPOS", "HDEL", "EXPIRE")
        )
        assert numkeys == 3
        stream_key, dedupe_key, order_key, dedupe_id, maxlen, ttl, event_type, payload = args
        cap = int(maxlen)
        ttl_seconds = int(ttl)
        marker = (str(dedupe_key), str(dedupe_id))
        if marker in self.dedupe:
            self._refresh_ttl(str(dedupe_key), ttl_seconds)
            self._refresh_ttl(str(order_key), ttl_seconds)
            return [self.dedupe[marker], 0]
        stream_id = f"{len(self.streams.get(str(stream_key), [])) + 1}-0"
        self.streams.setdefault(str(stream_key), []).append(
            (stream_id, {"event_type": str(event_type), "payload": str(payload)})
        )
        self.dedupe[marker] = stream_id
        order = self.dedupe_order.setdefault(str(order_key), [])
        order.append(str(dedupe_id))
        while len(order) > cap:
            evicted = order.pop(0)
            self.dedupe.pop((str(dedupe_key), evicted), None)
        self._refresh_ttl(str(dedupe_key), ttl_seconds)
        self._refresh_ttl(str(order_key), ttl_seconds)
        return [stream_id, 1]

    def _refresh_ttl(self, key: str, ttl_seconds: int) -> None:
        self.ttls[key] = ttl_seconds
        self.expire_calls[key] = self.expire_calls.get(key, 0) + 1


@pytest.mark.asyncio
async def test_event_service_append_once_returns_original_stream_id_on_replay() -> None:
    redis = LuaFakeRedis()
    service = EventService(redis)  # type: ignore[arg-type]

    first = await service.append_session_event_once(
        "session-1",
        "agent.finished",
        {"agent_name": "intake"},
        dedupe_id="outbox-id:0",
    )
    replay = await service.append_session_event_once(
        "session-1",
        "agent.finished",
        {"agent_name": "intake"},
        dedupe_id="outbox-id:0",
    )

    assert replay.event_id == first.event_id
    assert first.deduplicated is False
    assert replay.deduplicated is True
    assert len(redis.streams["xuanhu:events:session-1"]) == 1


@pytest.mark.asyncio
async def test_event_service_append_once_caps_markers_and_refreshes_sliding_ttl() -> None:
    redis = LuaFakeRedis()
    service = EventService(redis, dedupe_ttl_seconds=37)  # type: ignore[arg-type]

    for index in range(10):
        await service.append_session_event_once(
            "session-bounded",
            "agent.finished",
            {"agent_name": "intake"},
            dedupe_id=f"outbox-{index}:0",
            maxlen=3,
        )

    dedupe_key = "xuanhu:events:dedupe:session-bounded"
    order_key = f"{dedupe_key}:order"
    assert redis.dedupe_order[order_key] == ["outbox-7:0", "outbox-8:0", "outbox-9:0"]
    assert {marker for key, marker in redis.dedupe if key == dedupe_key} == {
        "outbox-7:0",
        "outbox-8:0",
        "outbox-9:0",
    }
    assert redis.ttls == {dedupe_key: 37, order_key: 37}

    stream_count = len(redis.streams["xuanhu:events:session-bounded"])
    replay = await service.append_session_event_once(
        "session-bounded",
        "agent.finished",
        {"agent_name": "intake"},
        dedupe_id="outbox-9:0",
        maxlen=3,
    )
    assert replay.deduplicated is True
    assert len(redis.streams["xuanhu:events:session-bounded"]) == stream_count
    assert redis.expire_calls[dedupe_key] == 11
    assert redis.expire_calls[order_key] == 11
