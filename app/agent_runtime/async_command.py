"""Durable async command repository for R6-A.

A separate substrate from ``HttpCommandClaim``. Commands are enqueued under an
idempotency key, claimed by a worker with PostgreSQL ``FOR UPDATE SKIP LOCKED``,
and settled under an owner-token fence. Every externally meaningful lifecycle
transition (enqueue/claim/succeed/fail) writes a versioned, privacy-minimal
Outbox row in the same transaction.

The private ``request_payload`` and ``result_payload`` are private domain data
and may contain protected health information. ``request_payload`` is returned
only inside :class:`ClaimedCommand` for worker dispatch; neither is ever exposed
through the status API, Outbox/SSE, metrics, or any representation-oriented DTO.
``error_payload`` is a sanitized empty object in R6-A: regardless of what a
caller passes to ``fail``, exactly ``{}`` is persisted.
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.exceptions import (
    IdempotencyConflictError,
    SessionBusyError,
    SessionNotFoundError,
)
from app.models.async_command import ASYNC_COMMAND_OPERATIONS, AsyncCommand
from app.models.consult import ConsultSession
from app.models.domain import OutboxEvent

STATUS_QUEUED = "queued"
STATUS_RUNNING = "running"
STATUS_SUCCEEDED = "succeeded"
STATUS_FAILED = "failed"
TERMINAL_STATUSES = frozenset({STATUS_SUCCEEDED, STATUS_FAILED})
ACTIVE_STATUSES = frozenset({STATUS_QUEUED, STATUS_RUNNING})

_LEASE_OWNER_SAFE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")

_COMMAND_OUTBOX_STATE_VERSION = 1
_COMMAND_TRACE_PREFIX = "async-command:"

# The four external lifecycle event versions the Outbox publisher maps.
OUTBOX_EVENT_QUEUED = "async_command.queued.v1"
OUTBOX_EVENT_RUNNING = "async_command.running.v1"
OUTBOX_EVENT_SUCCEEDED = "async_command.succeeded.v1"
OUTBOX_EVENT_FAILED = "async_command.failed.v1"

# The single explicit, finite allowlist of failure codes that may be persisted
# to the public error_code column and projected to clients. Dynamic handler
# codes are mapped onto this fixed bucket (unknown codes collapse to UNKNOWN);
# repository.fail and the Outbox mapper reject anything not in this set so the
# public surface can never carry an arbitrary uppercase string.
#
# R6-B adds the finite set of deterministic business outcomes that the intake /
# advance / review handlers may surface as terminal command failures. Each is a
# PHI-free, finite decision code drawn from the existing synchronous API surface
# (``app.core.exceptions``), so async and sync expose the same semantics. The
# retryable infra bucket ``HANDLER_UNAVAILABLE`` remains the only retryable
# infrastructure code; transient business states (``SESSION_BUSY`` /
# ``INVALID_STATE_VERSION``) are retryable by their natural contract.
ASYNC_COMMAND_ERROR_CODES = frozenset(
    {
        # infra buckets
        "UNKNOWN_OPERATION",
        "HANDLER_UNEXPECTED",
        "HANDLER_REJECTED",
        "HANDLER_UNAVAILABLE",
        "ATTEMPTS_EXHAUSTED",
        "UNKNOWN",
        # deterministic business outcomes (PHI-safe, finite)
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
    }
)


class AsyncCommandErrorCode(StrEnum):
    """Payload-free failure codes for infra-level async-command errors."""

    SESSION_NOT_FOUND = "SESSION_NOT_FOUND"
    TRANSACTION_FAILED = "TRANSACTION_FAILED"


class AsyncCommandRepositoryError(RuntimeError):
    """A payload-free async-command repository failure with a stable code."""

    def __init__(self, code: AsyncCommandErrorCode) -> None:
        super().__init__(code.value)
        self.code = code


class AsyncCommandRef(BaseModel):
    """Public-safe handle returned by ``enqueue`` (never carries payload)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    command_id: uuid.UUID
    operation: str
    status: str
    attempt_count: int = Field(ge=0)
    replayed: bool


class ClaimedCommand(BaseModel):
    """Worker-only carrier that includes the private request payload."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    command_id: uuid.UUID
    session_id: uuid.UUID
    operation: str
    attempt_count: int = Field(ge=1)
    lease_token: uuid.UUID
    # Private worker-dispatch payload (may contain PHI). Never leave this
    # repository boundary except into the allowlisted worker dispatch.
    # repr=False keeps it out of logs/reprs that capture the DTO.
    request_payload: dict[str, Any] = Field(repr=False)


class AsyncCommandStatus(BaseModel):
    """Public-safe projection for the status API.

    Only the identifiers, status, attempt count, result HTTP status and the
    fixed error code are projected. The private ``result_payload`` and
    ``error_payload`` DB fields (which may carry R6-B domain output) are never
    copied into this DTO, so they cannot reach the status API, its repr, or any
    ``model_dump``.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    command_id: uuid.UUID
    operation: str
    status: str
    attempt_count: int = Field(ge=0)
    result_http_status: int | None = None
    error_code: str | None = None
    created_at: datetime | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None
    updated_at: datetime | None = None


class AsyncCommandRepository(Protocol):
    async def enqueue(
        self,
        *,
        session_id: uuid.UUID,
        operation: str,
        idempotency_key: str,
        request_payload: dict[str, Any],
    ) -> AsyncCommandRef: ...

    async def get_status(self, session_id: uuid.UUID, command_id: uuid.UUID) -> AsyncCommandStatus | None: ...

    async def session_exists(self, session_id: uuid.UUID) -> bool: ...

    async def claim(
        self,
        *,
        worker_id: str,
        limit: int,
        lease_seconds: int,
        max_attempts: int,
    ) -> tuple[ClaimedCommand, ...]: ...

    async def renew_lease(
        self,
        command_id: uuid.UUID,
        *,
        worker_id: str,
        lease_token: uuid.UUID,
        lease_seconds: int,
    ) -> bool: ...

    async def complete(
        self,
        command_id: uuid.UUID,
        *,
        worker_id: str,
        lease_token: uuid.UUID,
        http_status: int,
        result_payload: dict[str, Any],
    ) -> bool: ...

    async def fail(
        self,
        command_id: uuid.UUID,
        *,
        worker_id: str,
        lease_token: uuid.UUID,
        error_code: str,
        error_payload: dict[str, Any],
    ) -> bool: ...

    async def retry(
        self,
        command_id: uuid.UUID,
        *,
        worker_id: str,
        lease_token: uuid.UUID,
        retry_after_seconds: int,
    ) -> bool: ...


def canonical_json_digest(value: object) -> str:
    """Return the canonical SHA-256 hex digest of a JSON value."""
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


class PostgresAsyncCommandRepository:
    """Async SQLAlchemy repository whose concurrency relies on PostgreSQL."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    # ------------------------------------------------------------------
    # enqueue / get
    # ------------------------------------------------------------------

    async def enqueue(
        self,
        *,
        session_id: uuid.UUID,
        operation: str,
        idempotency_key: str,
        request_payload: dict[str, Any],
    ) -> AsyncCommandRef:
        self._validate_enqueue(operation, idempotency_key, request_payload)
        key_digest = _digest_text(idempotency_key)
        request_digest = canonical_json_digest(request_payload)
        command_id = uuid.uuid4()

        # A bounded retry loop tolerates the unavoidable INSERT race on the two
        # unique boundaries (logical key / active-per-session). Each pass opens
        # its own transaction and re-reads the logical key first, so a replay
        # resolves deterministically. The race is raised as ``_EnqueueRetry``
        # from *inside* the transaction (rolling it back) but caught *outside*
        # the transaction context below, so a dirty/aborted session can never
        # be reused across attempts.
        for _ in range(_ENQUEUE_RETRIES):
            try:
                return await self._enqueue_once(
                    session_id=session_id,
                    operation=operation,
                    key_digest=key_digest,
                    request_digest=request_digest,
                    command_id=command_id,
                    request_payload=request_payload,
                )
            except _EnqueueRetry:
                continue
        raise SessionBusyError(
            detail=f"session_id={session_id} already has an active async command",
            retryable=True,
        )

    async def _enqueue_once(
        self,
        *,
        session_id: uuid.UUID,
        operation: str,
        key_digest: str,
        request_digest: str,
        command_id: uuid.UUID,
        request_payload: dict[str, Any],
    ) -> AsyncCommandRef:
        """One transaction attempt at inserting a logical command.

        Returns a replay reference when the logical key already exists, or the
        fresh reference after a successful insert. A unique-boundary race
        (concurrent logical-key insert, or another active command on the same
        session) surfaces as :class:`_EnqueueRetry` after rollback.
        """
        async with self._session_factory() as session, session.begin():
            existing = await session.scalar(
                select(AsyncCommand)
                .where(
                    AsyncCommand.session_id == session_id,
                    AsyncCommand.operation == operation,
                    AsyncCommand.idempotency_key_digest == key_digest,
                )
                .with_for_update()
            )
            if existing is not None:
                if existing.request_digest != request_digest:
                    raise IdempotencyConflictError(
                        message="相同幂等键不能用于不同请求",
                        detail=(
                            f"session_id={session_id} operation={operation} "
                            f"idempotency_key_digest={key_digest} request_digest_mismatch"
                        ),
                        retryable=False,
                    )
                return self._ref(existing, replayed=True)

            locked_session = await session.get(ConsultSession, session_id, with_for_update=True)
            if locked_session is None:
                raise SessionNotFoundError(
                    detail=f"session_id={session_id} 在数据库中未找到",
                    retryable=False,
                )

            session.add(
                AsyncCommand(
                    id=command_id,
                    session_id=session_id,
                    operation=operation,
                    idempotency_key_digest=key_digest,
                    request_digest=request_digest,
                    request_payload=dict(request_payload),
                    status=STATUS_QUEUED,
                    attempt_count=0,
                )
            )
            self._add_outbox(
                session,
                event_type=OUTBOX_EVENT_QUEUED,
                session_id=session_id,
                command_id=command_id,
                operation=operation,
                status=STATUS_QUEUED,
                attempt=0,
            )
            try:
                await session.flush()
            except IntegrityError:
                # Same-session active boundary or a concurrent logical key
                # insert. The transaction rolls back on exit; the caller's loop
                # re-reads the key in a fresh transaction.
                raise _EnqueueRetry from None
            return AsyncCommandRef(
                command_id=command_id,
                operation=operation,
                status=STATUS_QUEUED,
                attempt_count=0,
                replayed=False,
            )

    async def get_status(self, session_id: uuid.UUID, command_id: uuid.UUID) -> AsyncCommandStatus | None:
        """Session-scoped read; cross-session lookups are indistinguishable."""
        try:
            async with self._session_factory() as session:
                row = await session.get(AsyncCommand, command_id)
                if row is None or row.session_id != session_id:
                    return None
                return self._status(row)
        except SQLAlchemyError:
            raise AsyncCommandRepositoryError(AsyncCommandErrorCode.TRANSACTION_FAILED) from None

    async def session_exists(self, session_id: uuid.UUID) -> bool:
        """Whether a consult session row exists (for status-API envelope routing)."""
        try:
            async with self._session_factory() as session:
                return await session.get(ConsultSession, session_id) is not None
        except SQLAlchemyError:
            raise AsyncCommandRepositoryError(AsyncCommandErrorCode.TRANSACTION_FAILED) from None

    # ------------------------------------------------------------------
    # claim / lease / settle (all owner-fenced)
    # ------------------------------------------------------------------

    async def claim(
        self,
        *,
        worker_id: str,
        limit: int,
        lease_seconds: int,
        max_attempts: int,
    ) -> tuple[ClaimedCommand, ...]:
        self._validate_worker(worker_id)
        if limit < 1 or lease_seconds < 1:
            raise ValueError("limit and lease_seconds must be positive")
        if max_attempts < 1:
            raise ValueError("max_attempts must be positive")
        try:
            async with self._session_factory() as session, session.begin():
                now = func.now()
                rows = (
                    await session.scalars(
                        select(AsyncCommand)
                        .where(
                            or_(
                                and_(AsyncCommand.status == STATUS_QUEUED, AsyncCommand.available_at <= now),
                                and_(AsyncCommand.status == STATUS_RUNNING, AsyncCommand.lease_expires_at <= now),
                            )
                        )
                        .order_by(AsyncCommand.created_at, AsyncCommand.id)
                        .limit(limit)
                        .with_for_update(skip_locked=True)
                    )
                ).all()
                claimed: list[ClaimedCommand] = []
                for row in rows:
                    # A crash/lease-expiry reclaim (or a corrupt queued row) whose
                    # attempt budget is already exhausted must never be dispatched
                    # again. Terminal-fail it atomically with a fixed code and a
                    # failed Outbox row instead of reclaiming it.
                    if row.attempt_count >= max_attempts:
                        self._exhaust_claim(session, row, now=now, attempt=row.attempt_count)
                        continue
                    first_start = row.started_at is None
                    row.status = STATUS_RUNNING
                    row.attempt_count += 1
                    row.lease_owner = worker_id
                    row.lease_token = uuid.uuid4()
                    row.lease_expires_at = now + timedelta(seconds=lease_seconds)
                    if first_start:
                        row.started_at = now
                    claimed.append(
                        ClaimedCommand(
                            command_id=row.id,
                            session_id=row.session_id,
                            operation=row.operation,
                            attempt_count=row.attempt_count,
                            lease_token=row.lease_token,
                            request_payload=dict(row.request_payload),
                        )
                    )
                for item in claimed:
                    self._add_outbox(
                        session,
                        event_type=OUTBOX_EVENT_RUNNING,
                        session_id=item.session_id,
                        command_id=item.command_id,
                        operation=item.operation,
                        status=STATUS_RUNNING,
                        attempt=item.attempt_count,
                    )
                await session.flush()
                return tuple(claimed)
        except SQLAlchemyError:
            raise AsyncCommandRepositoryError(AsyncCommandErrorCode.TRANSACTION_FAILED) from None

    @staticmethod
    def _exhaust_claim(
        session: AsyncSession,
        row: AsyncCommand,
        *,
        now: Any,
        attempt: int,
    ) -> None:
        """Atomically terminal-fail an attempt-exhausted candidate in-place."""
        row.status = STATUS_FAILED
        row.error_code = "ATTEMPTS_EXHAUSTED"
        row.error_payload = {}
        row.result_http_status = None
        row.result_payload = None
        row.lease_owner = None
        row.lease_token = None
        row.lease_expires_at = None
        row.completed_at = now
        row.updated_at = now
        PostgresAsyncCommandRepository._add_outbox(
            session,
            event_type=OUTBOX_EVENT_FAILED,
            session_id=row.session_id,
            command_id=row.id,
            operation=row.operation,
            status=STATUS_FAILED,
            attempt=attempt,
            error_code="ATTEMPTS_EXHAUSTED",
        )

    async def renew_lease(
        self,
        command_id: uuid.UUID,
        *,
        worker_id: str,
        lease_token: uuid.UUID,
        lease_seconds: int,
    ) -> bool:
        self._validate_worker(worker_id)
        if lease_seconds < 1:
            raise ValueError("lease_seconds must be positive")
        try:
            async with self._session_factory() as session, session.begin():
                result = await session.execute(
                    update(AsyncCommand)
                    .where(
                        AsyncCommand.id == command_id,
                        AsyncCommand.status == STATUS_RUNNING,
                        AsyncCommand.lease_owner == worker_id,
                        AsyncCommand.lease_token == lease_token,
                    )
                    .values(
                        lease_expires_at=func.now() + timedelta(seconds=lease_seconds),
                        updated_at=func.now(),
                    )
                    .returning(AsyncCommand.id)
                )
                return result.scalar_one_or_none() is not None
        except SQLAlchemyError:
            raise AsyncCommandRepositoryError(AsyncCommandErrorCode.TRANSACTION_FAILED) from None

    async def complete(
        self,
        command_id: uuid.UUID,
        *,
        worker_id: str,
        lease_token: uuid.UUID,
        http_status: int,
        result_payload: dict[str, Any],
    ) -> bool:
        self._validate_worker(worker_id)
        if http_status < 100 or http_status > 599:
            raise ValueError("http_status must be in 100..599")
        if not isinstance(result_payload, dict):
            raise ValueError("result_payload must be a JSON object")
        try:
            async with self._session_factory() as session, session.begin():
                row = (
                    await session.execute(
                        update(AsyncCommand)
                        .where(
                            AsyncCommand.id == command_id,
                            AsyncCommand.status == STATUS_RUNNING,
                            AsyncCommand.lease_owner == worker_id,
                            AsyncCommand.lease_token == lease_token,
                        )
                        .values(
                            status=STATUS_SUCCEEDED,
                            result_http_status=http_status,
                            result_payload=dict(result_payload),
                            error_code=None,
                            error_payload=None,
                            lease_owner=None,
                            lease_token=None,
                            lease_expires_at=None,
                            completed_at=func.now(),
                            updated_at=func.now(),
                        )
                        .returning(
                            AsyncCommand.operation,
                            AsyncCommand.attempt_count,
                            AsyncCommand.session_id,
                        )
                    )
                ).one_or_none()
                if row is None:
                    return False  # ownership lost
                self._add_outbox(
                    session,
                    event_type=OUTBOX_EVENT_SUCCEEDED,
                    session_id=row.session_id,
                    command_id=command_id,
                    operation=row.operation,
                    status=STATUS_SUCCEEDED,
                    attempt=row.attempt_count,
                )
                await session.flush()
                return True
        except SQLAlchemyError:
            raise AsyncCommandRepositoryError(AsyncCommandErrorCode.TRANSACTION_FAILED) from None

    async def fail(
        self,
        command_id: uuid.UUID,
        *,
        worker_id: str,
        lease_token: uuid.UUID,
        error_code: str,
        error_payload: dict[str, Any],
    ) -> bool:
        """Terminally fail an owned running command.

        ``error_payload`` is a sanitized empty object in R6-A. The argument is
        accepted for protocol compatibility and type-validated, then its content
        is deliberately discarded: a direct repository caller (bypassing the
        worker's own sanitizer) can never persist an arbitrary, possibly
        PHI-bearing payload. Exactly ``{}`` is written to the durable column.
        """
        self._validate_worker(worker_id)
        if error_code not in ASYNC_COMMAND_ERROR_CODES:
            raise ValueError(f"error_code must be one of the fixed allowlist: {sorted(ASYNC_COMMAND_ERROR_CODES)}")
        if not isinstance(error_payload, dict):
            raise ValueError("error_payload must be a JSON object")
        # Discard the caller's content after type validation. Only the sanitized
        # empty object is persisted, regardless of what was passed in.
        del error_payload
        sanitized_error_payload: dict[str, Any] = {}
        try:
            async with self._session_factory() as session, session.begin():
                row = (
                    await session.execute(
                        update(AsyncCommand)
                        .where(
                            AsyncCommand.id == command_id,
                            AsyncCommand.status == STATUS_RUNNING,
                            AsyncCommand.lease_owner == worker_id,
                            AsyncCommand.lease_token == lease_token,
                        )
                        .values(
                            status=STATUS_FAILED,
                            error_code=error_code,
                            error_payload=sanitized_error_payload,
                            result_http_status=None,
                            result_payload=None,
                            lease_owner=None,
                            lease_token=None,
                            lease_expires_at=None,
                            completed_at=func.now(),
                            updated_at=func.now(),
                        )
                        .returning(
                            AsyncCommand.operation,
                            AsyncCommand.attempt_count,
                            AsyncCommand.session_id,
                        )
                    )
                ).one_or_none()
                if row is None:
                    return False  # ownership lost
                self._add_outbox(
                    session,
                    event_type=OUTBOX_EVENT_FAILED,
                    session_id=row.session_id,
                    command_id=command_id,
                    operation=row.operation,
                    status=STATUS_FAILED,
                    attempt=row.attempt_count,
                    error_code=error_code,
                )
                await session.flush()
                return True
        except SQLAlchemyError:
            raise AsyncCommandRepositoryError(AsyncCommandErrorCode.TRANSACTION_FAILED) from None

    async def retry(
        self,
        command_id: uuid.UUID,
        *,
        worker_id: str,
        lease_token: uuid.UUID,
        retry_after_seconds: int,
    ) -> bool:
        """Return a transiently failed command to ``queued`` after backoff.

        This is an externally meaningful running -> queued transition, so a
        ``async_command.queued.v1`` Outbox row is written in the SAME
        transaction as the status flip, carrying the current attempt count.
        Ownership is required so a stale owner can never put a command it no
        longer holds back on the queue.
        """
        self._validate_worker(worker_id)
        if retry_after_seconds < 0:
            raise ValueError("retry_after_seconds must not be negative")
        try:
            async with self._session_factory() as session, session.begin():
                row = (
                    await session.execute(
                        update(AsyncCommand)
                        .where(
                            AsyncCommand.id == command_id,
                            AsyncCommand.status == STATUS_RUNNING,
                            AsyncCommand.lease_owner == worker_id,
                            AsyncCommand.lease_token == lease_token,
                        )
                        .values(
                            status=STATUS_QUEUED,
                            lease_owner=None,
                            lease_token=None,
                            lease_expires_at=None,
                            available_at=func.now() + timedelta(seconds=retry_after_seconds),
                            updated_at=func.now(),
                        )
                        .returning(
                            AsyncCommand.operation,
                            AsyncCommand.attempt_count,
                            AsyncCommand.session_id,
                        )
                    )
                ).one_or_none()
                if row is None:
                    return False  # ownership lost
                self._add_outbox(
                    session,
                    event_type=OUTBOX_EVENT_QUEUED,
                    session_id=row.session_id,
                    command_id=command_id,
                    operation=row.operation,
                    status=STATUS_QUEUED,
                    attempt=row.attempt_count,
                )
                await session.flush()
                return True
        except SQLAlchemyError:
            raise AsyncCommandRepositoryError(AsyncCommandErrorCode.TRANSACTION_FAILED) from None

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _validate_enqueue(operation: str, idempotency_key: str, request_payload: dict[str, Any]) -> None:
        if not isinstance(operation, str) or operation not in ASYNC_COMMAND_OPERATIONS:
            raise ValueError(f"operation must be one of the fixed allowlist: {sorted(ASYNC_COMMAND_OPERATIONS)}")
        if not isinstance(idempotency_key, str) or not idempotency_key or len(idempotency_key) > 200:
            raise ValueError("idempotency_key must contain 1..200 characters")
        if not isinstance(request_payload, dict):
            raise ValueError("request_payload must be a JSON object")

    @staticmethod
    def _validate_worker(worker_id: str) -> None:
        if not isinstance(worker_id, str) or _LEASE_OWNER_SAFE.fullmatch(worker_id) is None:
            raise ValueError("worker_id must match ^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")

    @staticmethod
    def _ref(row: AsyncCommand, *, replayed: bool) -> AsyncCommandRef:
        return AsyncCommandRef(
            command_id=row.id,
            operation=row.operation,
            status=row.status,
            attempt_count=row.attempt_count,
            replayed=replayed,
        )

    @staticmethod
    def _status(row: AsyncCommand) -> AsyncCommandStatus:
        # Only public-safe fields are projected. The private result_payload /
        # error_payload DB columns are deliberately not read here.
        return AsyncCommandStatus(
            command_id=row.id,
            operation=row.operation,
            status=row.status,
            attempt_count=row.attempt_count,
            result_http_status=row.result_http_status,
            error_code=row.error_code,
            created_at=row.created_at,
            started_at=row.started_at,
            completed_at=row.completed_at,
            updated_at=row.updated_at,
        )

    @staticmethod
    def _add_outbox(
        session: AsyncSession,
        *,
        event_type: str,
        session_id: uuid.UUID,
        command_id: uuid.UUID,
        operation: str,
        status: str,
        attempt: int,
        error_code: str | None = None,
    ) -> None:
        payload: dict[str, object] = {
            "command_id": str(command_id),
            "operation": operation,
            "status": status,
            "attempt": attempt,
        }
        if error_code is not None:
            payload["error_code"] = error_code
        session.add(
            OutboxEvent(
                id=uuid.uuid4(),
                event_type=event_type,
                session_id=session_id,
                graph_run_id=None,
                state_version=_COMMAND_OUTBOX_STATE_VERSION,
                trace_id=f"{_COMMAND_TRACE_PREFIX}{command_id}",
                payload=payload,
                status="pending",
                attempt_count=0,
            )
        )


class _EnqueueRetry(Exception):
    """Internal control flow to retry an enqueue after a unique-race rollback."""


# Number of bounded enqueue attempts across independent transactions. Each
# retry re-reads the logical key, so the outcome (replay vs SessionBusy) is
# deterministic.
_ENQUEUE_RETRIES = 3


def _digest_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


__all__ = [
    "STATUS_QUEUED",
    "STATUS_RUNNING",
    "STATUS_SUCCEEDED",
    "STATUS_FAILED",
    "OUTBOX_EVENT_QUEUED",
    "OUTBOX_EVENT_RUNNING",
    "OUTBOX_EVENT_SUCCEEDED",
    "OUTBOX_EVENT_FAILED",
    "ASYNC_COMMAND_ERROR_CODES",
    "ASYNC_COMMAND_OPERATIONS",
    "AsyncCommandErrorCode",
    "AsyncCommandRepositoryError",
    "AsyncCommandRef",
    "AsyncCommandStatus",
    "ClaimedCommand",
    "AsyncCommandRepository",
    "PostgresAsyncCommandRepository",
    "canonical_json_digest",
]
