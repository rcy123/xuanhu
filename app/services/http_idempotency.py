"""Durable idempotency orchestration for public HTTP write commands."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, cast

from sqlalchemy import null as sql_null
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.exceptions import (
    HttpCommandRecoveryRequiredError,
    HttpCommandReplayError,
    IdempotencyConflictError,
    ModelGatewayError,
    SessionBusyError,
    XuanhuError,
)
from app.db.session import get_session_factory
from app.models.http_command import HttpCommandClaim
from app.services.lease_guard import LeaseGuard, LeaseOwnershipLostError

HTTP_COMMAND_LEASE_SECONDS = 90
HTTP_COMMAND_HEARTBEAT_SECONDS = 20
HTTP_COMMAND_WAIT_SECONDS = 130

logger = logging.getLogger("xuanhu.http_idempotency")


@dataclass(frozen=True, slots=True)
class HttpCommandResult:
    """Replayable success data returned to an API route."""

    data: dict[str, Any]
    status_code: int
    message: str
    replayed: bool


@dataclass(frozen=True, slots=True)
class _OwnerClaim:
    claim_id: uuid.UUID
    owner_token: uuid.UUID


class HttpCommandExecutor:
    """Claim, execute once, persist the outcome, and replay later attempts.

    The claim lives in an independent short transaction so another process can
    observe it while the business operation is running.  Business state is
    committed before the success response is published.  If the owner process
    disappears in the narrow interval between those commits, the heartbeat
    expires and the claim becomes ``ambiguous``; retries fail closed instead of
    executing a possibly-applied clinical write again.

    The handler runs under the shared monotonic :class:`LeaseGuard`, which fences
    the operation end to end: it confirms ownership before the handler starts,
    keeps lease observation active through the business ``commit`` (which now runs
    inside the guarded operation), and requires one final bounded owner
    confirmation before any result is settled. On known ownership loss (owner
    token or status lost, or the deadline exhausted) the guard cancels/drains the
    handler and the executor best-effort rolls back any uncommitted stale write
    and marks the claim ambiguous, raising ``HTTP_COMMAND_RECOVERY_REQUIRED`` so a
    stale owner never completes a clinical write after its lease was reclaimed.
    Cleanup failures are logged by bounded type only; the internal lease error is
    never exposed.
    """

    def __init__(
        self,
        db: AsyncSession,
        *,
        session_factory: async_sessionmaker[AsyncSession] | None = None,
        lease_seconds: int = HTTP_COMMAND_LEASE_SECONDS,
        heartbeat_seconds: float = HTTP_COMMAND_HEARTBEAT_SECONDS,
        renew_attempt_seconds: float | None = None,
    ) -> None:
        if not 0 < heartbeat_seconds < lease_seconds:
            raise ValueError("heartbeat_seconds must be in (0, lease_seconds)")
        if renew_attempt_seconds is not None and renew_attempt_seconds <= 0:
            raise ValueError("renew_attempt_seconds must be positive")
        self._db = db
        self._factory = session_factory or get_session_factory()
        self._lease_seconds = lease_seconds
        self._heartbeat_seconds = heartbeat_seconds
        # Bound a single renew attempt independently of the heartbeat cadence.
        # Defaults to the heartbeat; operators may decouple it so a real DB renew
        # (typically tens of ms) can complete even under a tight watchdog.
        self._renew_attempt_seconds = renew_attempt_seconds

    async def execute(
        self,
        *,
        operation: str,
        scope_key: str,
        concurrency_scope: str | None,
        idempotency_key: str,
        is_idempotent: bool,
        request_payload: dict[str, Any],
        success_status: int,
        success_message: str,
        handler: Callable[[], Awaitable[dict[str, Any]]],
        durable_outcome_resolver: Callable[[], Awaitable[dict[str, Any] | None]] | None = None,
    ) -> HttpCommandResult:
        """Execute a public write under a durable claim and scope lock.

        Headerless requests carry a fresh server-generated key and are stored
        as ``non_idempotent``.  They therefore participate in concurrency
        control without exposing a caller-replayable logical command.
        """

        key_digest = _digest_text(idempotency_key)
        request_digest = _digest_json(request_payload)
        decision = await self._claim_or_replay(
            operation=operation,
            scope_key=scope_key,
            concurrency_scope=concurrency_scope,
            idempotency_mode="public" if is_idempotent else "non_idempotent",
            key_digest=key_digest,
            request_digest=request_digest,
            success_status=success_status,
            success_message=success_message,
            durable_outcome_resolver=durable_outcome_resolver,
        )
        if isinstance(decision, HttpCommandResult):
            return decision

        guard = LeaseGuard(
            heartbeat_seconds=self._heartbeat_seconds,
            lease_seconds=self._lease_seconds,
            renew_attempt_seconds=self._renew_attempt_seconds,
        )

        async def _guarded_operation() -> dict[str, Any]:
            # Keep lease observation active through the business commit so a
            # stale owner can never commit a clinical write after its lease was
            # reclaimed. A commit exception still rolls back / marks ambiguous.
            data = await handler()
            await self._db.commit()
            return data

        try:
            data = await guard.run(
                operation=_guarded_operation,
                renew=lambda: self._renew(decision),
            )
        except LeaseOwnershipLostError:
            # Ownership was lost (or the local deadline exhausted) — possibly after
            # the business commit completed. Fail closed: best-effort rollback and
            # ambiguous-marking never leak internal DB/lease details; durable-outcome
            # repair handles the already-applied narrow-window state. The internal
            # lease error is never exposed; only the stable PHI-safe public error.
            await self._safe_rollback()
            await self._safe_mark_ambiguous(decision)
            raise HttpCommandRecoveryRequiredError(
                detail="http_command ownership was lost during execution",
                retryable=False,
            ) from None
        except XuanhuError as exc:
            await self._safe_rollback()
            await self._complete_error(decision, _xuanhu_error_payload(exc))
            raise
        except ModelGatewayError as exc:
            await self._safe_rollback()
            await self._complete_error(decision, _model_gateway_error_payload(exc))
            raise
        except asyncio.CancelledError:
            await self._safe_rollback()
            await self._safe_mark_ambiguous(decision)
            raise
        except Exception:
            await self._safe_rollback()
            await self._safe_mark_ambiguous(decision)
            raise

        # The guard already ran a final bounded owner confirmation before returning
        # its result; ``_complete_success`` independently re-verifies ownership
        # atomically, so the success write also fails closed on any late loss.
        await self._complete_success(
            decision,
            data=data,
            status_code=success_status,
            message=success_message,
        )
        return HttpCommandResult(
            data=data,
            status_code=success_status,
            message=success_message,
            replayed=False,
        )

    async def _claim_or_replay(
        self,
        *,
        operation: str,
        scope_key: str,
        concurrency_scope: str | None,
        idempotency_mode: str,
        key_digest: str,
        request_digest: str,
        success_status: int,
        success_message: str,
        durable_outcome_resolver: Callable[[], Awaitable[dict[str, Any] | None]] | None,
    ) -> _OwnerClaim | HttpCommandResult:
        owner = _OwnerClaim(uuid.uuid4(), uuid.uuid4())
        inserted = await self._try_insert(
            owner,
            operation=operation,
            scope_key=scope_key,
            concurrency_scope=concurrency_scope,
            idempotency_mode=idempotency_mode,
            key_digest=key_digest,
            request_digest=request_digest,
        )
        if inserted:
            return owner

        existing = await self._load_logical_claim(operation, scope_key, key_digest)
        if existing is None:
            await self._handle_concurrency_conflict(concurrency_scope)
            # The in-flight scope may have completed between lookup and retry.
            owner = _OwnerClaim(uuid.uuid4(), uuid.uuid4())
            if await self._try_insert(
                owner,
                operation=operation,
                scope_key=scope_key,
                concurrency_scope=concurrency_scope,
                idempotency_mode=idempotency_mode,
                key_digest=key_digest,
                request_digest=request_digest,
            ):
                return owner
            existing = await self._load_logical_claim(operation, scope_key, key_digest)
            if existing is None:
                raise SessionBusyError(
                    detail=f"scope={scope_key} already has an in-flight HTTP command",
                    retryable=True,
                )

        self._validate_digest(existing, request_digest)
        return await self._resolve_existing(
            existing.id,
            request_digest,
            success_status=success_status,
            success_message=success_message,
            durable_outcome_resolver=durable_outcome_resolver,
        )

    async def _try_insert(
        self,
        owner: _OwnerClaim,
        *,
        operation: str,
        scope_key: str,
        concurrency_scope: str | None,
        idempotency_mode: str,
        key_digest: str,
        request_digest: str,
    ) -> bool:
        async with self._factory() as db:
            db.add(
                HttpCommandClaim(
                    id=owner.claim_id,
                    operation=operation,
                    scope_key=scope_key,
                    concurrency_scope=concurrency_scope,
                    idempotency_mode=idempotency_mode,
                    idempotency_key_digest=key_digest,
                    request_digest=request_digest,
                    status="running",
                    owner_token=owner.owner_token,
                    lease_expires_at=self._lease_deadline(),
                )
            )
            try:
                await db.commit()
            except IntegrityError:
                await db.rollback()
                return False
        return True

    async def _load_logical_claim(
        self,
        operation: str,
        scope_key: str,
        key_digest: str,
    ) -> HttpCommandClaim | None:
        async with self._factory() as db:
            return cast(
                HttpCommandClaim | None,
                await db.scalar(
                    select(HttpCommandClaim).where(
                        HttpCommandClaim.operation == operation,
                        HttpCommandClaim.scope_key == scope_key,
                        HttpCommandClaim.idempotency_key_digest == key_digest,
                    )
                ),
            )

    async def _handle_concurrency_conflict(self, concurrency_scope: str | None) -> None:
        if concurrency_scope is None:
            return
        async with self._factory() as db, db.begin():
            claim = await db.scalar(
                select(HttpCommandClaim)
                .where(
                    HttpCommandClaim.concurrency_scope == concurrency_scope,
                    HttpCommandClaim.status == "running",
                )
                .with_for_update()
            )
            if claim is None:
                return
            if _lease_expired(claim):
                claim.status = "ambiguous"
                claim.lease_expires_at = None
                claim.updated_at = datetime.now(UTC)
                return
            raise SessionBusyError(
                detail=f"scope={concurrency_scope} already has an in-flight HTTP command",
                retryable=True,
            )

    async def _resolve_existing(
        self,
        claim_id: uuid.UUID,
        request_digest: str,
        *,
        success_status: int,
        success_message: str,
        durable_outcome_resolver: Callable[[], Awaitable[dict[str, Any] | None]] | None,
    ) -> HttpCommandResult:
        deadline = asyncio.get_running_loop().time() + HTTP_COMMAND_WAIT_SECONDS
        while True:
            async with self._factory() as db:
                claim = await db.get(HttpCommandClaim, claim_id)
            if claim is None:
                raise HttpCommandRecoveryRequiredError(
                    detail=f"http_command_claim={claim_id} disappeared",
                    retryable=False,
                )
            self._validate_digest(claim, request_digest)
            resolved = _resolved_claim(claim)
            if resolved is not None:
                return resolved
            may_repair_from_durable = claim.status in {"ambiguous", "failed"} or (
                claim.status == "running" and _lease_expired(claim)
            )
            if durable_outcome_resolver is not None and may_repair_from_durable:
                durable_data = await durable_outcome_resolver()
                if durable_data is not None:
                    repaired = await self._repair_durable_success(
                        claim_id,
                        request_digest=request_digest,
                        data=durable_data,
                        status_code=success_status,
                        message=success_message,
                    )
                    if repaired is not None:
                        return repaired
                    continue
            if claim.status == "failed":
                raise HttpCommandReplayError(dict(claim.error_payload or {}))
            if claim.status == "ambiguous":
                raise HttpCommandRecoveryRequiredError(
                    detail=f"http_command_claim={claim_id} is ambiguous",
                    retryable=False,
                )
            if _lease_expired(claim):
                if await self._expire_claim(claim_id):
                    raise HttpCommandRecoveryRequiredError(
                        detail=f"http_command_claim={claim_id} lease expired",
                        retryable=False,
                    )
                # A heartbeat or completion may have won the row lock after
                # the stale snapshot above.  Reload instead of incorrectly
                # turning a healthy command into an ambiguous response.
                continue
            if asyncio.get_running_loop().time() >= deadline:
                raise SessionBusyError(
                    detail=f"http_command_claim={claim_id} is still running",
                    retryable=True,
                )
            await asyncio.sleep(0.1)

    async def _repair_durable_success(
        self,
        claim_id: uuid.UUID,
        *,
        request_digest: str,
        data: dict[str, Any],
        status_code: int,
        message: str,
    ) -> HttpCommandResult | None:
        """Publish a verified business outcome over an abandoned HTTP claim."""

        async with self._factory() as db, db.begin():
            claim = await db.get(HttpCommandClaim, claim_id, with_for_update=True)
            if claim is None:
                return None
            self._validate_digest(claim, request_digest)
            resolved = _resolved_claim(claim)
            if resolved is not None:
                return resolved
            repairable = claim.status in {"ambiguous", "failed"} or (
                claim.status == "running" and _lease_expired(claim)
            )
            if not repairable:
                return None
            claim.status = "completed"
            claim.http_status = status_code
            claim.response_payload = {"data": data, "message": message}
            claim.error_payload = cast(Any, sql_null())
            claim.lease_expires_at = None
            claim.completed_at = datetime.now(UTC)
            claim.updated_at = datetime.now(UTC)
        return HttpCommandResult(
            data=data,
            status_code=status_code,
            message=message,
            replayed=True,
        )

    async def _expire_claim(self, claim_id: uuid.UUID) -> bool:
        async with self._factory() as db, db.begin():
            claim = await db.get(HttpCommandClaim, claim_id, with_for_update=True)
            if claim is not None and claim.status == "running" and _lease_expired(claim):
                claim.status = "ambiguous"
                claim.lease_expires_at = None
                claim.updated_at = datetime.now(UTC)
                return True
        return False

    async def _renew(self, owner: _OwnerClaim) -> bool:
        """Renew the owner-fenced lease; ``False`` means owner/status was lost.

        The shared lease guard calls this under the monotonic watchdog. A
        transient failure is retried by the guard while the local deadline
        remains; a ``False`` result (or deadline exhaustion) makes the guard
        cancel/drain the business handler so a stale owner never keeps writing.
        """
        async with self._factory() as db, db.begin():
            claim = await db.get(HttpCommandClaim, owner.claim_id, with_for_update=True)
            if claim is None or claim.status != "running" or claim.owner_token != owner.owner_token:
                return False
            claim.lease_expires_at = self._lease_deadline()
            claim.updated_at = datetime.now(UTC)
        return True

    def _lease_deadline(self) -> datetime:
        """Wall-clock lease deadline using this executor's configured lease."""
        return datetime.now(UTC) + timedelta(seconds=self._lease_seconds)

    async def _complete_success(
        self,
        owner: _OwnerClaim,
        *,
        data: dict[str, Any],
        status_code: int,
        message: str,
    ) -> None:
        async with self._factory() as db, db.begin():
            claim = await db.get(HttpCommandClaim, owner.claim_id, with_for_update=True)
            _verify_owner(claim, owner)
            assert claim is not None
            claim.status = "completed"
            claim.http_status = status_code
            claim.response_payload = {"data": data, "message": message}
            claim.error_payload = cast(Any, sql_null())
            claim.lease_expires_at = None
            claim.completed_at = datetime.now(UTC)
            claim.updated_at = datetime.now(UTC)

    async def _complete_error(
        self,
        owner: _OwnerClaim,
        error_payload: dict[str, Any],
    ) -> None:
        async with self._factory() as db, db.begin():
            claim = await db.get(HttpCommandClaim, owner.claim_id, with_for_update=True)
            _verify_owner(claim, owner)
            assert claim is not None
            claim.status = "failed"
            claim.http_status = int(error_payload.get("status_code") or 500)
            claim.error_payload = error_payload
            claim.response_payload = cast(Any, sql_null())
            claim.lease_expires_at = None
            claim.completed_at = datetime.now(UTC)
            claim.updated_at = datetime.now(UTC)

    async def _mark_ambiguous(self, owner: _OwnerClaim) -> None:
        async with self._factory() as db, db.begin():
            claim = await db.get(HttpCommandClaim, owner.claim_id, with_for_update=True)
            if claim is not None and claim.status == "running" and claim.owner_token == owner.owner_token:
                claim.status = "ambiguous"
                claim.lease_expires_at = None
                claim.updated_at = datetime.now(UTC)

    async def _safe_rollback(self) -> None:
        """Best-effort rollback that never leaks a cleanup failure.

        A failing rollback must not replace the stable public error with an
        internal DB exception. Cancellation is allowed through only so an
        external cancel still wins; every other failure is logged with just its
        bounded type name.
        """
        try:
            await asyncio.shield(self._db.rollback())
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("http-command lease-loss rollback failed: %s", type(exc).__name__)

    async def _safe_mark_ambiguous(self, owner: _OwnerClaim) -> None:
        """Best-effort ambiguous-marking that never leaks a cleanup failure.

        If the ambiguous-mark cannot be written it is only logged by bounded type
        name; the stable fail-closed public error still surfaces, and durable-outcome
        repair can recover the claim later.
        """
        try:
            await asyncio.shield(self._mark_ambiguous(owner))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("http-command lease-loss ambiguous-mark failed: %s", type(exc).__name__)

    @staticmethod
    def _validate_digest(claim: HttpCommandClaim, request_digest: str) -> None:
        if claim.request_digest != request_digest:
            raise IdempotencyConflictError(
                detail=(f"operation={claim.operation} scope={claim.scope_key} request_digest_mismatch"),
                retryable=False,
            )


def session_http_scope(session_id: str) -> str:
    """Return a bounded canonical concurrency scope for a session path value."""

    try:
        return f"session:{uuid.UUID(session_id)}"
    except ValueError:
        return f"session:invalid:{_digest_text(session_id)}"


def _resolved_claim(claim: HttpCommandClaim) -> HttpCommandResult | None:
    if claim.status != "completed" or claim.response_payload is None or claim.http_status is None:
        return None
    payload = dict(claim.response_payload)
    data = payload.get("data")
    if not isinstance(data, dict):
        raise HttpCommandRecoveryRequiredError(
            detail=f"http_command_claim={claim.id} has invalid response payload",
            retryable=False,
        )
    return HttpCommandResult(
        data=data,
        status_code=claim.http_status,
        message=str(payload.get("message") or "ok"),
        replayed=True,
    )


def _verify_owner(claim: HttpCommandClaim | None, owner: _OwnerClaim) -> None:
    if claim is None or claim.status != "running" or claim.owner_token != owner.owner_token:
        raise HttpCommandRecoveryRequiredError(
            detail=f"http_command_claim={owner.claim_id} ownership was lost",
            retryable=False,
        )


def _xuanhu_error_payload(exc: XuanhuError) -> dict[str, Any]:
    extras: dict[str, Any] = {}
    agent_error_code = getattr(exc, "agent_error_code", None)
    issues = getattr(exc, "issues", None)
    if agent_error_code is not None:
        extras["agent_error_code"] = agent_error_code
    if issues is not None:
        extras["issues"] = issues
    return {
        "code": str(exc.code),
        "message": exc.message,
        "detail": exc.detail,
        "retryable": exc.retryable,
        "status_code": exc.status_code,
        "extra_payload": extras,
    }


def _model_gateway_error_payload(exc: ModelGatewayError) -> dict[str, Any]:
    return {
        "code": "MODEL_GATEWAY_UNAVAILABLE",
        "message": "模型网关不可用",
        "detail": None,
        "retryable": exc.retryable,
        "status_code": 503,
        "extra_payload": {},
    }


def _digest_json(payload: dict[str, Any]) -> str:
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return _digest_text(canonical)


def _digest_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _lease_expired(claim: HttpCommandClaim) -> bool:
    lease = claim.lease_expires_at
    if lease is None:
        return True
    if lease.tzinfo is None:
        lease = lease.replace(tzinfo=UTC)
    return lease <= datetime.now(UTC)
