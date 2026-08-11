"""Deterministic unit tests for the shared monotonic lease guard (R8-B).

The guard races one operation against a periodic renew heartbeat. These tests
drive the guard directly with scripted ``renew`` callables so every ownership
outcome (healthy renew, known loss, transient failure recovery, deadline
exhaustion, stuck renew, external cancellation, simultaneous loss+completion) is
covered without touching a database. Real PostgreSQL integration is covered by
the ``integration`` marker in the worker / HTTP executor test modules.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable

import pytest

from app.services.lease_guard import LeaseGuard, LeaseOwnershipLostError

# Small, deterministic test timings. heartbeat must stay strictly below lease.
_HEARTBEAT = 0.02
_LEASE = 0.3


async def _run(op, renew: Callable, *, heartbeat: float = _HEARTBEAT, lease: float = _LEASE):
    return await LeaseGuard(heartbeat_seconds=heartbeat, lease_seconds=lease).run(
        operation=op,
        renew=renew,
    )


async def test_healthy_renew_allows_completion() -> None:
    async def op() -> str:
        await asyncio.sleep(0.05)
        return "done"

    result = await _run(op, renew=lambda: _renew(True))
    assert result == "done"


async def test_renew_false_before_start_prevents_handler() -> None:
    """A fenced start with ``False`` (known loss) must never enter the handler."""
    started = asyncio.Event()

    async def op() -> str:
        started.set()
        return "done"

    with pytest.raises(LeaseOwnershipLostError):
        await _run(op, renew=lambda: _renew(False))
    assert not started.is_set()  # the handler never started


async def test_renew_false_after_start_cancels_blocked_operation() -> None:
    """Ownership lost on a later heartbeat cancels a blocked, already-started op."""
    started = asyncio.Event()
    calls = {"n": 0}

    async def renew() -> bool:
        calls["n"] += 1
        return calls["n"] == 1  # preflight True, next heartbeat False

    async def op() -> str:
        started.set()
        await asyncio.Event().wait()  # blocked forever
        return "done"

    with pytest.raises(LeaseOwnershipLostError):
        await _run(op, renew=renew)
    assert started.is_set()  # entered after a confirmed preflight, then cancelled


async def test_transient_renew_error_followed_by_success_completes() -> None:
    calls = {"n": 0}

    async def renew() -> bool:
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("transient DB hiccup")
        return True

    async def op() -> str:
        await asyncio.sleep(0.06)  # outlive the transient failure heartbeat
        return "ok"

    result = await _run(op, renew=renew)
    assert result == "ok"
    assert calls["n"] >= 2


async def test_slow_renew_with_decoupled_attempt_budget_recovers() -> None:
    """A renew slower than the heartbeat cadence must still succeed.

    Regression for the R8-B integration bug: when the per-attempt budget
    defaults to the heartbeat, a real DB renewal (tens of ms) that is slower
    than a tight heartbeat is cancelled as a timeout on every attempt and never
    succeeds, exhausting the deadline. With ``renew_attempt_seconds`` decoupled
    to a generous budget, the same slow renewal completes and the transient
    failure is retried/recovered instead of failing closed.
    """
    calls = {"n": 0}

    async def renew() -> bool:
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("transient DB hiccup")
        # Simulate a real DB renewal transaction: slower than the 0.02s
        # heartbeat, but well within the 0.2s per-attempt budget.
        await asyncio.sleep(0.05)
        return True

    async def op() -> str:
        await asyncio.sleep(0.15)
        return "done"

    result = await LeaseGuard(
        heartbeat_seconds=_HEARTBEAT,  # 0.02s, tighter than the renew itself
        lease_seconds=0.5,
        renew_attempt_seconds=0.2,
    ).run(op, renew)
    assert result == "done"
    assert calls["n"] >= 2  # the transient failure was retried, not fatal


async def test_repeated_renew_errors_hit_deadline_and_raise() -> None:
    async def renew() -> bool:
        raise RuntimeError("permanently failing renew")

    async def op() -> str:
        await asyncio.Event().wait()
        return "never"

    with pytest.raises(LeaseOwnershipLostError):
        await _run(op, renew=renew)


async def test_transient_failures_back_off_not_hot_spin() -> None:
    """A fast always-failing renew must back off, never tight-spin.

    The preflight ``_confirm_ownership`` runs a paced retry loop: each fast
    failure sleeps a bounded interval (capped by heartbeat and remaining
    deadline) before retrying. If it spun with no delay, this test would fail
    closed in milliseconds with a huge call count; with the backoff it runs out
    the lease at a bounded retry rate.
    """
    calls = {"n": 0}

    async def renew() -> bool:
        calls["n"] += 1
        raise RuntimeError("fast transient failure")

    async def op() -> str:
        await asyncio.Event().wait()
        return "never"

    started = asyncio.get_running_loop().time()
    with pytest.raises(LeaseOwnershipLostError):
        await _run(op, renew=renew, heartbeat=_HEARTBEAT, lease=_LEASE)
    elapsed = asyncio.get_running_loop().time() - started
    # Ran out the lease (no tight spin would take milliseconds), yet stopped
    # within a small scheduler tolerance of it (no unbounded overshoot).
    assert elapsed >= _LEASE - 0.05
    assert elapsed < _LEASE + 0.15
    # Paced at ~lease/heartbeat intervals: a hot spin would be orders of
    # magnitude more calls in the same elapsed window.
    assert calls["n"] <= 40


async def test_stuck_renew_is_bounded_and_fails_closed_at_deadline() -> None:
    """A renew that hangs forever must fail closed at the tight local deadline."""

    async def renew() -> bool:
        await asyncio.Event().wait()  # stuck DB call
        return True

    async def op() -> str:
        await asyncio.Event().wait()
        return "never"

    started = asyncio.get_running_loop().time()
    with pytest.raises(LeaseOwnershipLostError):
        # renew_attempt bounded below the lease so the stuck call can't spin.
        await _run(op, renew=renew, heartbeat=_HEARTBEAT, lease=_LEASE)
    elapsed = asyncio.get_running_loop().time() - started
    # Fail closed at the lease, not a full heartbeat+attempt past it (small
    # scheduler tolerance). This is the strict-deadline regression guard.
    assert elapsed < _LEASE + 0.15


async def test_stuck_renew_in_watchdog_is_bounded_by_deadline() -> None:
    """A stuck heartbeat after a confirmed start must not outlive the deadline."""
    calls = {"n": 0}

    async def renew() -> bool:
        calls["n"] += 1
        if calls["n"] == 1:
            return True  # preflight confirms ownership, handler starts
        await asyncio.Event().wait()  # subsequent heartbeat is stuck
        return True

    async def op() -> str:
        await asyncio.Event().wait()
        return "never"

    started = asyncio.get_running_loop().time()
    with pytest.raises(LeaseOwnershipLostError):
        await _run(op, renew=renew, heartbeat=_HEARTBEAT, lease=_LEASE)
    elapsed = asyncio.get_running_loop().time() - started
    # The watchdog caps every sleep and attempt by the remaining deadline, so it
    # cannot carry a stuck handler a full extra heartbeat past the lease.
    assert elapsed < _LEASE + 0.15


async def test_external_cancellation_is_prompt_and_propagates() -> None:
    started = asyncio.Event()

    async def op() -> str:
        started.set()
        await asyncio.Event().wait()
        return "done"

    task = asyncio.create_task(_run(op, renew=lambda: _renew(True), heartbeat=_HEARTBEAT, lease=_LEASE))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=1)


async def test_operation_completion_and_simultaneous_loss_is_fail_closed() -> None:
    """If the operation completes in the same tick as a loss, loss wins."""
    calls = {"n": 0}
    release = asyncio.Event()

    async def renew() -> bool:
        calls["n"] += 1
        if calls["n"] == 1:
            return True  # fenced start confirms ownership
        release.set()  # let the operation complete at the same tick
        return False  # ownership lost in the same tick

    async def op() -> str:
        await release.wait()
        return "stale-result"

    with pytest.raises(LeaseOwnershipLostError):
        await _run(op, renew=renew)


async def test_preflight_true_then_final_false_is_fail_closed() -> None:
    """If a fast operation finishes first, a failed final confirm must not settle it."""
    calls = {"n": 0}

    async def renew() -> bool:
        calls["n"] += 1
        return calls["n"] == 1  # preflight True, final confirmation False

    async def op() -> str:
        return "done"  # completes before any heartbeat

    with pytest.raises(LeaseOwnershipLostError):
        await _run(op, renew=renew)


async def test_handler_exception_is_preserved_unchanged() -> None:
    class _BusinessError(Exception):
        pass

    async def op() -> str:
        raise _BusinessError("handler fault")

    with pytest.raises(_BusinessError):
        await _run(op, renew=lambda: _renew(True))


async def test_invalid_timing_rejected() -> None:
    with pytest.raises(ValueError):
        LeaseGuard(heartbeat_seconds=_LEASE, lease_seconds=_LEASE)  # not strictly below
    with pytest.raises(ValueError):
        LeaseGuard(heartbeat_seconds=_HEARTBEAT, lease_seconds=_HEARTBEAT)
    with pytest.raises(ValueError):
        LeaseGuard(heartbeat_seconds=_HEARTBEAT, lease_seconds=_LEASE, renew_attempt_seconds=0)


async def _renew(value: bool):
    return value


async def test_ownership_loss_error_is_payload_free() -> None:
    async def op() -> str:
        await asyncio.Event().wait()
        return "never"

    with pytest.raises(LeaseOwnershipLostError) as captured:
        await _run(op, renew=lambda: _renew(False))
    # No args, no payload, no PHI, no high-cardinality text in the repr.
    assert not captured.value.args
    assert repr(captured.value) == "LeaseOwnershipLostError()"
