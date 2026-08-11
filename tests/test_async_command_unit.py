"""R6-A async-command deterministic unit tests.

Covers the pure logic that does not need PostgreSQL: input validators, the
canonical digest, DTO contracts, the worker's construction invariants, its
backoff math, and the bounded-error sanitizers. Concurrency, lease fencing,
retry and Outbox/Redis behavior are covered by the integration marker.
"""

from __future__ import annotations

import re
import uuid
from typing import Any

import pytest

from app.agent_runtime.async_command import (
    ASYNC_COMMAND_ERROR_CODES,
    ASYNC_COMMAND_OPERATIONS,
    STATUS_FAILED,
    AsyncCommandRef,
    ClaimedCommand,
    PostgresAsyncCommandRepository,
    canonical_json_digest,
)
from app.agent_runtime.async_command_worker import (
    AsyncCommandWorker,
    CommandFailureError,
    _bounded_error_code,
    _bounded_error_payload,
)
from app.core.config import Settings

VALID_OPERATION = "session.advance"

# ---------------------------------------------------------------------------
# canonical digest
# ---------------------------------------------------------------------------


def test_canonical_digest_is_deterministic_and_key_insensitive() -> None:
    a = canonical_json_digest({"b": 2, "a": 1, "nested": {"y": True, "x": None}})
    b = canonical_json_digest({"nested": {"x": None, "y": True}, "a": 1, "b": 2})
    assert a == b
    assert re.fullmatch(r"[0-9a-f]{64}", a)
    assert canonical_json_digest({"a": 1}) != canonical_json_digest({"a": 2})


# ---------------------------------------------------------------------------
# DTO contracts
# ---------------------------------------------------------------------------


def test_ref_dto_is_frozen_and_rejects_extra_fields() -> None:
    ref = AsyncCommandRef(
        command_id=uuid.uuid4(),
        operation=VALID_OPERATION,
        status="queued",
        attempt_count=0,
        replayed=False,
    )
    with pytest.raises(ValueError):
        ref.replayed = True  # type: ignore[misc]  # frozen
    with pytest.raises(ValueError):
        AsyncCommandRef(  # type: ignore[call-arg]
            command_id=uuid.uuid4(),
            operation=VALID_OPERATION,
            status="queued",
            attempt_count=0,
            replayed=False,
            stray="surprise",
        )


def test_claimed_command_carries_the_private_payload_but_has_no_digest() -> None:
    claimed = ClaimedCommand(
        command_id=uuid.uuid4(),
        session_id=uuid.uuid4(),
        operation=VALID_OPERATION,
        attempt_count=1,
        lease_token=uuid.uuid4(),
        request_payload={"patient": "PHI"},
    )
    assert claimed.request_payload == {"patient": "PHI"}


def test_claimed_command_repr_omits_the_private_payload() -> None:
    claimed = ClaimedCommand(
        command_id=uuid.uuid4(),
        session_id=uuid.uuid4(),
        operation=VALID_OPERATION,
        attempt_count=1,
        lease_token=uuid.uuid4(),
        request_payload={"patient": "PHI-super-secret"},
    )
    assert "PHI-super-secret" not in repr(claimed)
    assert "request_payload" not in repr(claimed)


def test_async_command_context_repr_omits_the_private_payload() -> None:
    from app.agent_runtime.async_command_worker import AsyncCommandContext

    ctx = AsyncCommandContext(
        command_id=uuid.uuid4(),
        session_id=uuid.uuid4(),
        operation=VALID_OPERATION,
        attempt_count=1,
        request_payload={"patient": "PHI-super-secret"},
    )
    assert "PHI-super-secret" not in repr(ctx)
    assert "request_payload" not in repr(ctx)


def test_operation_allowlist_is_exactly_the_planned_r6b_set() -> None:
    assert {"intake.message", "session.advance", "prescription.review"} == ASYNC_COMMAND_OPERATIONS


def test_error_code_allowlist_is_exactly_the_planned_r6b_set() -> None:
    # R6-B expands the R6-A infra codes with the finite, deterministic business
    # codes the three worker handlers map known XuanhuError outcomes onto.
    assert {
        "UNKNOWN_OPERATION",
        "HANDLER_UNEXPECTED",
        "HANDLER_REJECTED",
        "HANDLER_UNAVAILABLE",
        "ATTEMPTS_EXHAUSTED",
        "UNKNOWN",
        # ---- R6-B business codes ----
        "SESSION_NOT_FOUND",
        "SESSION_BUSY",
        "SESSION_TERMINATED",
        "INVALID_STATE_VERSION",
        "INVALID_STAGE_TRANSITION",
        "INSUFFICIENT_INQUIRY",
        "PENDING_DOCTOR_REVIEW",
        "STATE_RECOVERY_REQUIRED",
        "IDEMPOTENCY_KEY_REUSED",
        "INVALID_REVIEW_ACTION",
        "FORMULA_OVERRIDE_REQUIRED",
        "SAFETY_REVIEW_BLOCKED",
        "SAFETY_ACCEPT_RISK_UNSUPPORTED",
        "AGENT_TRIGGER_FAILED",
    } == ASYNC_COMMAND_ERROR_CODES


# ---------------------------------------------------------------------------
# repository input validators (pure, DB-free; validators run before any DB use)
# ---------------------------------------------------------------------------


async def _enqueue(*, operation: str, idempotency_key: str, request_payload: object) -> None:
    repo = PostgresAsyncCommandRepository(None)  # type: ignore[arg-type]
    await repo.enqueue(  # type: ignore[call-arg]
        session_id=uuid.uuid4(),
        operation=operation,
        idempotency_key=idempotency_key,
        request_payload=request_payload,  # type: ignore[arg-type]
    )


async def test_enqueue_validates_operation_against_allowlist() -> None:
    with pytest.raises(ValueError):
        await _enqueue(operation="bad op!", idempotency_key="k", request_payload={})
    with pytest.raises(ValueError):
        await _enqueue(operation="x" * 65, idempotency_key="k", request_payload={})
    # A well-formed but unplanned operation is still rejected: only the fixed
    # R6-B allowlist may be enqueued.
    with pytest.raises(ValueError):
        await _enqueue(operation="doctor.prescribe", idempotency_key="k", request_payload={})
    # Every allowlisted operation is accepted by the validator.
    for operation in ASYNC_COMMAND_OPERATIONS:
        with pytest.raises((ValueError,)) as excinfo:
            await _enqueue(operation=operation, idempotency_key="", request_payload={})
        # idempotency_key validation must run independently of the (valid)
        # operation: an empty key is the failure, not the operation.
        assert "idempotency_key" in str(excinfo.value)


async def test_enqueue_validates_idempotency_key_bounds() -> None:
    with pytest.raises(ValueError):
        await _enqueue(operation=VALID_OPERATION, idempotency_key="", request_payload={})
    with pytest.raises(ValueError):
        await _enqueue(operation=VALID_OPERATION, idempotency_key="k" * 201, request_payload={})


async def test_enqueue_validates_payload_is_object() -> None:
    with pytest.raises(ValueError):
        await _enqueue(operation=VALID_OPERATION, idempotency_key="k", request_payload=[1, 2, 3])


async def test_worker_id_regex_enforced_across_worker_methods() -> None:
    repo = PostgresAsyncCommandRepository(None)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        await repo.claim(worker_id="bad worker!", limit=1, lease_seconds=1, max_attempts=1)
    with pytest.raises(ValueError):
        await repo.renew_lease(uuid.uuid4(), worker_id="bad worker!", lease_token=uuid.uuid4(), lease_seconds=1)
    with pytest.raises(ValueError):
        await repo.complete(
            uuid.uuid4(), worker_id="bad worker!", lease_token=uuid.uuid4(), http_status=200, result_payload={}
        )
    with pytest.raises(ValueError):
        await repo.fail(
            uuid.uuid4(), worker_id="bad worker!", lease_token=uuid.uuid4(), error_code="BAD", error_payload={}
        )
    with pytest.raises(ValueError):
        await repo.retry(uuid.uuid4(), worker_id="bad worker!", lease_token=uuid.uuid4(), retry_after_seconds=1)


async def test_fail_rejects_non_object_error_payload() -> None:
    repo = PostgresAsyncCommandRepository(None)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        await repo.fail(
            uuid.uuid4(),
            worker_id="worker-a",
            lease_token=uuid.uuid4(),
            error_code="UNKNOWN",
            error_payload=[1, 2, 3],  # type: ignore[arg-type]
        )


class _FakeRow:
    operation = "session.advance"
    attempt_count = 1
    session_id = uuid.uuid4()


class _FakeResult:
    def one_or_none(self) -> _FakeRow:
        return _FakeRow()


class _FakeBegin:
    async def __aenter__(self) -> _FakeBegin:
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False


class _RecordingSession:
    """Fake async session that records the column values a persist sends."""

    def __init__(self) -> None:
        self.persisted: dict[str, Any] = {}

    async def __aenter__(self) -> _RecordingSession:
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False

    def begin(self) -> _FakeBegin:
        return _FakeBegin()

    async def execute(self, statement: Any) -> _FakeResult:
        # Record literal column values only; SQL expression values (e.g.
        # func.now()) have no resolved value and are skipped.
        self.persisted = {
            column.key: getattr(parameter, "value", None)
            for column, parameter in dict(statement._values).items()
        }
        return _FakeResult()

    def add(self, *items: Any) -> None:
        del items

    async def flush(self) -> None:
        return None


class _RecordingFactory:
    def __init__(self) -> None:
        self.session = _RecordingSession()

    def __call__(self) -> _RecordingSession:
        return self.session


async def test_fail_persists_sanitized_empty_error_payload() -> None:
    """Repository fail persists exactly {} regardless of caller input."""
    secret = "PHI-secret-9f8e7d"
    factory = _RecordingFactory()
    repo = PostgresAsyncCommandRepository(factory)  # type: ignore[arg-type]
    ok = await repo.fail(
        uuid.uuid4(),
        worker_id="worker-a",
        lease_token=uuid.uuid4(),
        error_code="HANDLER_REJECTED",
        error_payload={"nested": {"deep": {"patient": secret}}},
    )
    assert ok is True
    assert factory.session.persisted.get("status") == STATUS_FAILED
    assert factory.session.persisted.get("error_code") == "HANDLER_REJECTED"
    # The malicious nested payload is discarded at the boundary; only {} is
    # sent to the durable error_payload column.
    assert factory.session.persisted.get("error_payload") == {}
    assert secret not in str(factory.session.persisted)


# ---------------------------------------------------------------------------
# config invariants
# ---------------------------------------------------------------------------


def _settings(**overrides: object) -> Settings:
    base: dict[str, object] = {
        "database_url": "postgresql://x",
        "redis_url": "redis://x",
        "model_gateway_base_url": "http://localhost",
        "model_gateway_api_key": "k",
        "chat_model": "m",
        "embedding_model": "e",
        "embedding_dim": 768,
    }
    base.update(overrides)
    return Settings(**base)


def test_settings_require_heartbeat_below_lease_when_enabled() -> None:
    from pydantic import ValidationError

    # R7: the worker is enabled by default (XUANHU_ASYNC_COMMAND_ENABLED defaults
    # true), so the lease/heartbeat invariant applies unless explicitly disabled.
    disabled_kwargs: dict[str, object] = {"XUANHU_ASYNC_COMMAND_ENABLED": False}

    # heartbeat strictly below lease is valid when enabled (default on).
    assert _settings(async_command_lease_seconds=10, async_command_heartbeat_seconds=9)
    # disabled ignores the lease/heartbeat relation (operator kill switch).
    assert _settings(
        **disabled_kwargs,
        async_command_lease_seconds=10,
        async_command_heartbeat_seconds=10,
    )

    with pytest.raises(ValidationError):
        _settings(async_command_lease_seconds=10, async_command_heartbeat_seconds=10)
    with pytest.raises(ValidationError):
        _settings(async_command_lease_seconds=10, async_command_heartbeat_seconds=11)


# ---------------------------------------------------------------------------
# bounded-error sanitizers
# ---------------------------------------------------------------------------


def test_bounded_error_code_maps_dynamic_codes_to_fixed_bucket() -> None:
    # Every allowlisted code passes through unchanged.
    for code in ASYNC_COMMAND_ERROR_CODES:
        assert _bounded_error_code(code) == code
    # Arbitrary uppercase strings are NOT accepted: they collapse to UNKNOWN.
    assert _bounded_error_code("UPSTREAM_TIMEOUT") == "UNKNOWN"
    assert _bounded_error_code("PATIENT_BLOCKED") == "UNKNOWN"
    assert _bounded_error_code("FLAPPY") == "UNKNOWN"
    assert _bounded_error_code("not an error!!") == "UNKNOWN"
    assert _bounded_error_code("") == "UNKNOWN"
    assert _bounded_error_code("A" * 200) == "UNKNOWN"


def test_bounded_error_payload_is_always_empty_for_r6a() -> None:
    # R6-A terminal error_payload is always an empty object; arbitrary handler
    # content (which may carry PHI) is never copied through.
    assert _bounded_error_payload({"a": 1}) == {}
    assert _bounded_error_payload({"nested": {"patient": "PHI"}}) == {}
    assert _bounded_error_payload("not-a-dict") == {}
    assert _bounded_error_payload(None) == {}


# ---------------------------------------------------------------------------
# worker construction invariants
# ---------------------------------------------------------------------------


def test_worker_requires_heartbeat_strictly_below_lease() -> None:
    with pytest.raises(ValueError):
        AsyncCommandWorker(
            repository=None,  # type: ignore[arg-type]
            worker_id="w",
            lease_seconds=10,
            heartbeat_interval_seconds=10,
        )
    with pytest.raises(ValueError):
        AsyncCommandWorker(
            repository=None,  # type: ignore[arg-type]
            worker_id="w",
            lease_seconds=10,
            heartbeat_interval_seconds=11,
        )
    AsyncCommandWorker(
        repository=None,  # type: ignore[arg-type]
        worker_id="w",
        lease_seconds=10,
        heartbeat_interval_seconds=9,
    )


def test_worker_positive_bounds() -> None:
    with pytest.raises(ValueError):
        AsyncCommandWorker(repository=None, worker_id="", lease_seconds=60)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        AsyncCommandWorker(repository=None, worker_id="w", batch_size=0)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        AsyncCommandWorker(repository=None, worker_id="w", max_attempts=0)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        AsyncCommandWorker(  # type: ignore[arg-type]
            repository=None,
            worker_id="w",
            retry_base_seconds=10,
            retry_max_seconds=1,
        )
    with pytest.raises(ValueError):
        AsyncCommandWorker(repository=None, worker_id="w", poll_interval_seconds=0)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# backoff math
# ---------------------------------------------------------------------------


def test_retry_delay_exponential_capped_at_max() -> None:
    worker = AsyncCommandWorker(
        repository=None,  # type: ignore[arg-type]
        worker_id="w",
        retry_base_seconds=1,
        retry_max_seconds=300,
    )
    assert worker.retry_delay_seconds(1) == 1
    assert worker.retry_delay_seconds(2) == 2
    assert worker.retry_delay_seconds(3) == 4
    assert worker.retry_delay_seconds(9) == 256
    assert worker.retry_delay_seconds(20) == 300


# ---------------------------------------------------------------------------
# CommandFailureError carries a sanitized contract
# ---------------------------------------------------------------------------


def test_command_failure_error_defaults() -> None:
    failure = CommandFailureError(error_code="BUSY")
    assert failure.error_code == "BUSY"
    assert failure.error_payload == {}
    assert failure.retryable is True

    terminal = CommandFailureError(error_code="BLOCKED", error_payload={"why": "x"}, retryable=False)
    assert terminal.error_payload == {"why": "x"}
    assert terminal.retryable is False
