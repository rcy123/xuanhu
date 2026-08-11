"""Shared monotonic lease watchdog for durable command ownership (R8-B).

Both the durable ``AsyncCommandWorker`` (R6-A/B) and the synchronous HTTP
idempotency ``HttpCommandExecutor`` (R1-R5) keep a lease alive while a business
handler runs, and both previously ignored ownership loss after the heartbeat
returned: a stale handler kept running clinical side effects even after its
lease expired or was reclaimed. This module is the single internal watchdog both
integrators use, so the subtle loops cannot drift apart.

The guard fences the operation end to end:

* **Fenced start** — before the operation may begin, the guard performs an
  immediate owner-fenced renewal/confirmation. ``False`` (known loss) means the
  handler never starts. A transient failure is retried only within the *initial*
  local deadline; a confirmed ``True`` resets the local deadline to a full lease.
* **Heartbeat** — the operation then races a periodic ``renew`` heartbeat. A
  successful ``renew()`` (``True``) extends the *local monotonic* deadline;
  ``False`` is a known ownership loss and cancels/drains the operation
  immediately; a transient ``renew`` failure (exception or a stuck call bounded
  by an individual attempt timeout) is retried only while the deadline remains.
  Every sleep and every renew attempt is capped by the remaining deadline, and
  the deadline is checked before an attempt and again immediately after it, so a
  stuck call or a failure near the deadline can never carry execution past it.
* **Final confirmation** — when the operation finishes first, the watchdog is
  stopped/drained and one final serialized bounded owner confirmation runs with
  the same deadline rules. Only a confirmed ``True`` lets the operation result
  (or handler exception) be returned/re-raised; ``False`` or deadline exhaustion
  wins, so a token lost just before/with handler completion never yields a stale
  result. Renew calls are never run concurrently.

Once the deadline is reached the operation is cancelled/drained and a single
payload-free :class:`LeaseOwnershipLostError` is raised so the caller can fail
closed and never settle a stale result.

The local watchdog uses the event loop's monotonic clock (``loop.time()``),
never wall time. Each ``renew`` attempt is independently bounded so a stuck DB
call cannot carry execution past the deadline. External cancellation cancels and
drains both tasks promptly and propagates ``CancelledError`` unchanged; if the
operation and an ownership loss complete together, ownership loss wins.

The guard attaches no request payload, exception text, idempotency key, trace,
clinical data or dynamic high-cardinality value to its error, logs or repr.
Timings are supplied only as keyword parameters; production callers pass their
own configured lease/heartbeat values.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Awaitable, Callable
from typing import TypeVar

T = TypeVar("T")


class LeaseOwnershipLostError(Exception):
    """Internal, payload-free signal that durable lease ownership was lost.

    Never surfaced to clients or into logs with any payload. Integrators
    translate it to a stable, PHI-safe public error of their own. No request
    payload, exception text, idempotency key, trace, clinical data or
    high-cardinality value is ever attached to this exception or its repr.
    """


class _Deadline:
    """Shared *local monotonic* lease deadline for the guard's fenced run.

    ``remaining()`` reads the event loop's monotonic clock; ``reset()`` restores
    a full lease from the current instant. Both the preflight/final confirmations
    and the periodic watchdog read and reset the same holder so the budget is
    enforced consistently across the whole operation.
    """

    __slots__ = ("_lease", "_expires")

    def __init__(self, now: float, lease: float) -> None:
        self._lease = lease
        self._expires = now + lease

    def remaining(self) -> float:
        return self._expires - asyncio.get_running_loop().time()

    def reset(self) -> None:
        self._expires = asyncio.get_running_loop().time() + self._lease


class LeaseGuard:
    """Race one ``operation`` against a periodic ``renew`` lease heartbeat."""

    def __init__(
        self,
        *,
        heartbeat_seconds: float,
        lease_seconds: float,
        renew_attempt_seconds: float | None = None,
    ) -> None:
        if not 0 < heartbeat_seconds < lease_seconds:
            raise ValueError("heartbeat_seconds must be in (0, lease_seconds)")
        if renew_attempt_seconds is not None and renew_attempt_seconds <= 0:
            raise ValueError("renew_attempt_seconds must be positive")
        self._heartbeat = heartbeat_seconds
        self._lease = lease_seconds
        self._renew_attempt = renew_attempt_seconds if renew_attempt_seconds is not None else heartbeat_seconds

    async def run(
        self,
        operation: Callable[[], Awaitable[T]],
        renew: Callable[[], Awaitable[bool]],
    ) -> T:
        """Run ``operation`` until it settles, ownership is lost, or it is cancelled.

        Returns the operation result (re-raising any handler exception unchanged),
        or raises :class:`LeaseOwnershipLostError` on known loss / deadline
        exhaustion, or propagates ``CancelledError`` on external cancellation.
        """
        loop = asyncio.get_running_loop()
        holder = _Deadline(loop.time(), self._lease)
        # Fenced start: confirm ownership before the handler may begin. ``False``
        # (known loss) means the handler never starts; transient renew failures
        # retry within the initial deadline; a confirmed ``True`` resets the local
        # deadline to a full lease.
        if not await self._confirm_ownership(renew, holder):
            raise LeaseOwnershipLostError()

        op_task = asyncio.create_task(self._run_operation(operation))
        watchdog = asyncio.create_task(self._watchdog(renew, holder))
        try:
            try:
                done, _pending = await asyncio.wait(
                    {op_task, watchdog},
                    return_when=asyncio.FIRST_COMPLETED,
                )
            except asyncio.CancelledError:
                # External cancellation: cancel/drain both promptly and propagate.
                self._cancel(op_task)
                self._cancel(watchdog)
                raise
            if watchdog in done:
                # Ownership loss wins even if the operation completed at the same
                # moment: never settle a stale result.
                self._cancel(op_task)
                exc = watchdog.exception()
                if isinstance(exc, LeaseOwnershipLostError):
                    raise exc
                if exc is not None:
                    raise exc
                raise LeaseOwnershipLostError()
            # Operation finished first: stop and drain the watchdog, then require
            # one final bounded owner confirmation before settling the result.
            self._cancel(watchdog)
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await watchdog
            if not await self._confirm_ownership(renew, holder):
                raise LeaseOwnershipLostError()
            # Only a confirmed owner may receive the handler result or re-raise the
            # handler exception unchanged.
            return await op_task
        finally:
            # No orphan tasks: cancel anything still pending and drain both.
            self._cancel(op_task)
            self._cancel(watchdog)
            for task in (op_task, watchdog):
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task

    async def _watchdog(
        self,
        renew: Callable[[], Awaitable[bool]],
        holder: _Deadline,
    ) -> None:
        while True:
            # Check the deadline BEFORE starting an attempt; cap the sleep so a
            # failure near the deadline cannot wait a full heartbeat.
            if holder.remaining() <= 0:
                raise LeaseOwnershipLostError()
            try:
                await asyncio.sleep(min(self._heartbeat, holder.remaining()))
            except asyncio.CancelledError:
                raise
            # Re-check the deadline immediately before the attempt and cap the
            # renew timeout by the remaining deadline.
            remaining = holder.remaining()
            if remaining <= 0:
                raise LeaseOwnershipLostError()
            try:
                renewed = await asyncio.wait_for(
                    self._run_renew(renew),
                    timeout=min(self._renew_attempt, remaining),
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                # A stuck renew attempt (bounded by the attempt timeout above) or
                # any transient failure: retry only while the deadline remains.
                continue
            if renewed is False:
                raise LeaseOwnershipLostError()
            if renewed is True:
                holder.reset()

    async def _confirm_ownership(
        self,
        renew: Callable[[], Awaitable[bool]],
        holder: _Deadline,
    ) -> bool:
        """Confirm owner-fenced lease ownership within the remaining deadline.

        Returns ``True`` only on a confirmed ``True`` renew (which also resets the
        local deadline to a full lease); returns ``False`` on known loss or when
        the deadline is exhausted. Transient renew failures retry within the
        remaining deadline. Cancellation propagates unchanged.
        """
        while True:
            if holder.remaining() <= 0:
                return False
            try:
                renewed = await asyncio.wait_for(
                    self._run_renew(renew),
                    timeout=min(self._renew_attempt, holder.remaining()),
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                # Transient renew failure: back off before retrying so a fast
                # always-failing renew (e.g. a DB outage) never hot-spins. The
                # sleep is capped by both the heartbeat and the remaining
                # deadline, and the deadline is re-checked after it, so a paced
                # retry can never carry execution past the lease. Cancellation
                # during the backoff propagates promptly.
                try:
                    await asyncio.sleep(min(self._heartbeat, holder.remaining()))
                except asyncio.CancelledError:
                    raise
                continue
            if renewed is False:
                return False
            if renewed is True:
                holder.reset()
                return True

    @staticmethod
    async def _run_operation(operation: Callable[[], Awaitable[T]]) -> T:
        """Adapt an ``Awaitable``-returning callable into a cancellable coroutine."""
        return await operation()

    @staticmethod
    async def _run_renew(renew: Callable[[], Awaitable[bool]]) -> bool:
        """Adapt an ``Awaitable``-returning renew into a cancellable coroutine."""
        return await renew()

    @staticmethod
    def _cancel(task: asyncio.Task[T]) -> None:
        if not task.done():
            task.cancel()


__all__ = ["LeaseGuard", "LeaseOwnershipLostError"]
