"""Generic durable async-command worker for R6-A.

The worker claims commands with ``FOR UPDATE SKIP LOCKED``, dispatches each to
an allowlisted handler with fresh (DB-independent) inputs, heartbeats long
handlers, and settles success/failure/retry under the owner-token fence. It
never uses an in-memory queue as a source of truth and never swallows
cancellation: on a forced cancel the lease expires and another worker reclaims.

Observation/publishing failures must not corrupt command state — a settle that
fails is rolled back atomically with its Outbox row, leaving the command
leased so a later worker finishes it after lease reclaim.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import uuid
from dataclasses import dataclass, field
from typing import Any, Protocol

from app.agent_runtime.async_command import (
    ASYNC_COMMAND_ERROR_CODES,
    ASYNC_COMMAND_OPERATIONS,
    ClaimedCommand,
    PostgresAsyncCommandRepository,
)
from app.agent_runtime.async_command import AsyncCommandRepository as AsyncCommandRepositoryProtocol
from app.services.lease_guard import LeaseGuard, LeaseOwnershipLostError

logger = logging.getLogger("xuanhu.async_command_worker")


@dataclass(frozen=True, slots=True)
class AsyncCommandContext:
    """Fresh, DB-independent handler inputs for one dispatch."""

    command_id: uuid.UUID
    session_id: uuid.UUID
    operation: str
    attempt_count: int
    # Private worker-dispatch payload (may contain PHI). Handlers are the only
    # trusted consumers and must never project it into errors, logs or events.
    # repr=False keeps it out of any repr/log that captures the context.
    request_payload: dict[str, Any] = field(repr=False)


@dataclass(frozen=True, slots=True)
class CommandSuccess:
    http_status: int
    result_payload: dict[str, Any]


class CommandFailureError(Exception):
    """A typed handler failure. ``retryable`` drives retry vs terminal fail."""

    def __init__(
        self,
        *,
        error_code: str,
        error_payload: dict[str, Any] | None = None,
        retryable: bool = True,
    ) -> None:
        super().__init__(error_code)
        self.error_code = error_code
        self.error_payload = error_payload if error_payload is not None else {}
        self.retryable = retryable


class AsyncCommandHandler(Protocol):
    async def __call__(self, context: AsyncCommandContext) -> CommandSuccess: ...


@dataclass(frozen=True, slots=True)
class WorkerRunResult:
    claimed: int = 0
    succeeded: int = 0
    failed: int = 0
    retried: int = 0
    ownership_lost: int = 0
    rejected: int = 0


class AsyncCommandWorker:
    """Poll/claim/dispatch/settle loop over an allowlisted handler registry."""

    def __init__(
        self,
        repository: PostgresAsyncCommandRepository | AsyncCommandRepositoryProtocol,
        handlers: dict[str, AsyncCommandHandler] | None = None,
        *,
        worker_id: str,
        batch_size: int = 10,
        lease_seconds: int = 60,
        heartbeat_interval_seconds: float = 20,
        max_attempts: int = 8,
        retry_base_seconds: int = 1,
        retry_max_seconds: int = 300,
        poll_interval_seconds: float = 0.5,
    ) -> None:
        if not worker_id or len(worker_id) > 128:
            raise ValueError("worker_id must contain 1..128 characters")
        if batch_size < 1 or lease_seconds < 1 or max_attempts < 1:
            raise ValueError("batch_size, lease_seconds and max_attempts must be positive")
        if not 0 < heartbeat_interval_seconds < lease_seconds:
            raise ValueError("heartbeat_interval_seconds must be in (0, lease_seconds)")
        if retry_base_seconds < 0 or retry_max_seconds < retry_base_seconds:
            raise ValueError("invalid retry interval bounds")
        if poll_interval_seconds <= 0:
            raise ValueError("poll_interval_seconds must be positive")
        unknown_operations = set(handlers or {}) - ASYNC_COMMAND_OPERATIONS
        if unknown_operations:
            raise ValueError(
                f"handler operations not in the allowlist: {sorted(unknown_operations)}"
            )
        self._repository = repository
        self._handlers = dict(handlers or {})
        self._worker_id = worker_id
        self._batch_size = batch_size
        self._lease_seconds = lease_seconds
        self._heartbeat_interval_seconds = heartbeat_interval_seconds
        self._max_attempts = max_attempts
        self._retry_base_seconds = retry_base_seconds
        self._retry_max_seconds = retry_max_seconds
        self._poll_interval_seconds = poll_interval_seconds

    @property
    def worker_id(self) -> str:
        return self._worker_id

    def retry_delay_seconds(self, attempt_count: int) -> int:
        exponent = min(63, max(0, attempt_count - 1))
        return min(self._retry_max_seconds, self._retry_base_seconds * (1 << exponent))

    async def run_once(self) -> WorkerRunResult:
        claimed_commands = await self._repository.claim(
            worker_id=self._worker_id,
            limit=self._batch_size,
            lease_seconds=self._lease_seconds,
            max_attempts=self._max_attempts,
        )
        counts: dict[str, int] = {}
        for command in claimed_commands:
            outcome = await self._process(command)
            counts[outcome] = counts.get(outcome, 0) + 1
        return WorkerRunResult(
            claimed=len(claimed_commands),
            succeeded=counts.get("succeeded", 0),
            failed=counts.get("failed", 0),
            retried=counts.get("retried", 0),
            ownership_lost=counts.get("ownership_lost", 0),
            rejected=counts.get("rejected", 0),
        )

    async def run_forever(self, stop: asyncio.Event) -> None:
        """Poll until stopped; always finish an already-claimed batch first."""
        while not stop.is_set():
            try:
                result = await self.run_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("async-command claim cycle failed: %s", type(exc).__name__)
                result = WorkerRunResult()
            if result.claimed:
                await asyncio.sleep(0)
                continue
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=self._poll_interval_seconds)

    # ------------------------------------------------------------------
    # dispatch / settle
    # ------------------------------------------------------------------

    async def _process(self, command: ClaimedCommand) -> str:
        handler = self._handlers.get(command.operation)
        if handler is None:
            # Unknown operations fail closed: no execution, terminal rejection.
            await self._settle_terminal(
                command,
                error_code="UNKNOWN_OPERATION",
                error_payload={},
            )
            return "rejected"
        context = AsyncCommandContext(
            command_id=command.command_id,
            session_id=command.session_id,
            operation=command.operation,
            attempt_count=command.attempt_count,
            request_payload=command.request_payload,
        )
        try:
            success = await self._dispatch_with_heartbeat(command, context, handler)
        except LeaseOwnershipLostError:
            # Ownership was lost or the local lease deadline was exhausted: the
            # handler was cancelled/drained. Never settle (complete/fail/retry) a
            # stale result — the current owner (or a later reclaim) is authoritative.
            return "ownership_lost"
        except CommandFailureError as failure:
            return await self._settle_failure(command, failure)
        except asyncio.CancelledError:
            # Never swallow cancellation: the lease expires and a worker
            # reclaims the command later.
            raise
        except Exception:
            # Unexpected handler crash. Persist no exception text (it may
            # contain PHI); treat as transient and backoff.
            return await self._settle_transient(command, error_code="HANDLER_UNEXPECTED")
        return await self._settle_success(command, success)

    async def _dispatch_with_heartbeat(
        self,
        command: ClaimedCommand,
        context: AsyncCommandContext,
        handler: AsyncCommandHandler,
    ) -> CommandSuccess:
        """Run the handler under the shared monotonic lease guard.

        On known ownership loss or local deadline exhaustion the guard cancels
        and drains the handler and raises :class:`LeaseOwnershipLostError`, so a
        stale handler can never continue clinical side effects after its lease
        was reclaimed. Healthy renewals and forced external cancellation behave
        exactly as before.
        """
        guard = LeaseGuard(
            heartbeat_seconds=self._heartbeat_interval_seconds,
            lease_seconds=self._lease_seconds,
        )
        return await guard.run(
            operation=lambda: handler(context),
            renew=lambda: self._renew(command),
        )

    async def _renew(self, command: ClaimedCommand) -> bool:
        """Renew the owner-fenced lease; ``False`` means ownership was lost."""
        return await self._repository.renew_lease(
            command.command_id,
            worker_id=self._worker_id,
            lease_token=command.lease_token,
            lease_seconds=self._lease_seconds,
        )

    async def _settle_success(self, command: ClaimedCommand, success: CommandSuccess) -> str:
        try:
            ok = await self._repository.complete(
                command.command_id,
                worker_id=self._worker_id,
                lease_token=command.lease_token,
                http_status=success.http_status,
                result_payload=success.result_payload,
            )
        except Exception as exc:
            logger.warning("async-command complete failed: %s", type(exc).__name__)
            return "ownership_lost"
        return "succeeded" if ok else "ownership_lost"

    async def _settle_failure(self, command: ClaimedCommand, failure: CommandFailureError) -> str:
        if failure.retryable and command.attempt_count < self._max_attempts:
            return await self._settle_transient(
                command,
                error_code=_bounded_error_code(failure.error_code),
            )
        return await self._settle_terminal(
            command,
            error_code=_bounded_error_code(failure.error_code),
            error_payload=_bounded_error_payload(failure.error_payload),
        )

    async def _settle_transient(self, command: ClaimedCommand, *, error_code: str) -> str:
        backoff = self.retry_delay_seconds(command.attempt_count)
        if command.attempt_count < self._max_attempts:
            try:
                ok = await self._repository.retry(
                    command.command_id,
                    worker_id=self._worker_id,
                    lease_token=command.lease_token,
                    retry_after_seconds=backoff,
                )
            except Exception as exc:
                logger.warning("async-command retry failed: %s", type(exc).__name__)
                return "ownership_lost"
            return "retried" if ok else "ownership_lost"
        # Attempts exhausted: persist a sanitized terminal failure.
        return await self._settle_terminal(
            command,
            error_code=_bounded_error_code(error_code),
            error_payload={},
        )

    async def _settle_terminal(self, command: ClaimedCommand, *, error_code: str, error_payload: dict[str, Any]) -> str:
        try:
            ok = await self._repository.fail(
                command.command_id,
                worker_id=self._worker_id,
                lease_token=command.lease_token,
                error_code=_bounded_error_code(error_code),
                error_payload=_bounded_error_payload(error_payload),
            )
        except Exception as exc:
            logger.warning("async-command fail settle failed: %s", type(exc).__name__)
            return "ownership_lost"
        return "failed" if ok else "ownership_lost"


def _bounded_error_code(code: str) -> str:
    """Map an arbitrary handler error code onto the fixed finite allowlist.

    Only the allowlisted codes pass through; anything else (including arbitrary
    uppercase strings, exception text, or empty values) collapses to ``UNKNOWN``.
    The public error_code surface can therefore only ever carry one of the fixed
    buckets, never an arbitrary value.
    """
    if code in ASYNC_COMMAND_ERROR_CODES:
        return code
    return "UNKNOWN"


def _bounded_error_payload(payload: dict[str, Any] | None) -> dict[str, Any]:
    """R6-A terminal error_payload is always an empty object.

    Arbitrary handler payload content (which may carry PHI) is deliberately not
    shallow-copied into the durable/publishable failure; it is discarded.
    """
    del payload
    return {}


def build_async_command_worker(
    repository: PostgresAsyncCommandRepository | AsyncCommandRepositoryProtocol,
    handlers: dict[str, AsyncCommandHandler] | None = None,
    **kwargs: Any,
) -> AsyncCommandWorker:
    """Factory so the FastAPI lifespan and tests share one construction path."""
    return AsyncCommandWorker(repository, handlers=handlers, **kwargs)


__all__ = [
    "AsyncCommandContext",
    "CommandSuccess",
    "CommandFailureError",
    "AsyncCommandHandler",
    "AsyncCommandWorker",
    "WorkerRunResult",
    "build_async_command_worker",
]
