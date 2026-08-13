"""R6-A worker dispatch unit tests with an in-memory fake repository.

Focuses on the worker's dispatch/settle decision tree: success, transient
retry, terminal fail, unknown-operation rejection, unexpected-handler-crash
handling, cancellation propagation and ownership-loss fencing. The fake
repository makes every settle step deterministic; real PostgreSQL concurrency
is covered by the integration marker.
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

import pytest

from app.agent_runtime.async_command import ClaimedCommand
from app.agent_runtime.async_command_worker import (
    AsyncCommandContext,
    AsyncCommandWorker,
    CommandFailureError,
    CommandSuccess,
)


def _claimed(
    *,
    attempt_count: int = 1,
    operation: str = "session.advance",
    session_id: uuid.UUID | None = None,
) -> ClaimedCommand:
    return ClaimedCommand(
        command_id=uuid.uuid4(),
        session_id=session_id or uuid.uuid4(),
        operation=operation,
        attempt_count=attempt_count,
        lease_token=uuid.uuid4(),
        request_payload={"patient": "PHI"},
    )


class _FakeRepository:
    """Records settle calls; claim yields a scripted queue."""

    def __init__(
        self,
        *,
        claim_results: list[ClaimedCommand] | None = None,
        renew: Any | None = None,
    ) -> None:
        self.queue = list(claim_results or [])
        self.claimed_limit: int | None = None
        self.completed: list[tuple[Any, Any, Any]] = []
        self.failed: list[tuple[Any, Any, Any]] = []
        self.retried: list[tuple[Any, Any, Any]] = []
        self.complete_result: bool = True
        self.fail_result: bool = True
        self.retry_result: bool = True
        # Optional scripted renew behavior. Default: ownership is always held.
        self._renew_fn = renew

    async def claim(self, *, worker_id: str, limit: int, lease_seconds: int, max_attempts: int) -> tuple[ClaimedCommand, ...]:
        self.claimed_limit = limit
        taken, self.queue = tuple(self.queue[:limit]), self.queue[limit:]
        return taken

    async def renew_lease(self, command_id, *, worker_id, lease_token, lease_seconds) -> bool:
        if self._renew_fn is not None:
            return await self._renew_fn(
                command_id,
                worker_id=worker_id,
                lease_token=lease_token,
                lease_seconds=lease_seconds,
            )
        return True

    async def complete(self, command_id, *, worker_id, lease_token, http_status, result_payload) -> bool:
        self.completed.append((command_id, http_status, result_payload))
        return self.complete_result

    async def fail(self, command_id, *, worker_id, lease_token, error_code, error_payload) -> bool:
        self.failed.append((command_id, error_code, error_payload))
        return self.fail_result

    async def retry(self, command_id, *, worker_id, lease_token, retry_after_seconds) -> bool:
        self.retried.append((command_id, retry_after_seconds))
        return self.retry_result


async def test_success_dispatch_settles_completed() -> None:
    async def handler(ctx: AsyncCommandContext) -> CommandSuccess:
        assert ctx.request_payload == {"patient": "PHI"}
        return CommandSuccess(http_status=200, result_payload={"ok": True})

    repo = _FakeRepository(claim_results=[_claimed()])
    worker = AsyncCommandWorker(repo, handlers={"session.advance": handler}, worker_id="w")
    result = await worker.run_once()

    assert result.claimed == 1
    assert result.succeeded == 1
    assert result.failed == 0
    assert len(repo.completed) == 1
    assert repo.completed[0][1] == 200
    assert repo.completed[0][2] == {"ok": True}


async def test_retryable_failure_below_max_attempts_retries() -> None:
    async def handler(ctx: AsyncCommandContext) -> CommandSuccess:
        raise CommandFailureError(error_code="UPSTREAM_TIMEOUT", retryable=True)

    repo = _FakeRepository(claim_results=[_claimed(attempt_count=2)])
    worker = AsyncCommandWorker(repo, handlers={"session.advance": handler}, worker_id="w", max_attempts=8)
    result = await worker.run_once()

    assert result.retried == 1
    assert len(repo.retried) == 1
    assert repo.retried[0][1] > 0  # deterministic backoff applied
    assert repo.failed == []


async def test_non_retryable_failure_settles_terminal() -> None:
    async def handler(ctx: AsyncCommandContext) -> CommandSuccess:
        # A dynamic handler code outside the allowlist is mapped to UNKNOWN.
        raise CommandFailureError(error_code="PATIENT_BLOCKED", retryable=False)

    repo = _FakeRepository(claim_results=[_claimed()])
    worker = AsyncCommandWorker(repo, handlers={"session.advance": handler}, worker_id="w")
    result = await worker.run_once()

    assert result.failed == 1
    assert len(repo.failed) == 1
    assert repo.failed[0][1] == "UNKNOWN"
    assert repo.failed[0][2] == {}  # R6-A terminal error_payload is empty
    assert repo.retried == []


async def test_transient_failure_exhausts_attempts_to_terminal_fail() -> None:
    async def handler(ctx: AsyncCommandContext) -> CommandSuccess:
        # FLAPPY is outside the fixed allowlist -> collapses to UNKNOWN.
        raise CommandFailureError(error_code="FLAPPY", retryable=True)

    repo = _FakeRepository(claim_results=[_claimed(attempt_count=8)])
    worker = AsyncCommandWorker(repo, handlers={"session.advance": handler}, worker_id="w", max_attempts=8)
    result = await worker.run_once()

    assert result.failed == 1
    assert result.retried == 0
    assert repo.failed == [(repo.failed[0][0], "UNKNOWN", {})]


async def test_unknown_operation_fails_closed_without_execution() -> None:
    """A missing handler must never dispatch; it is rejected terminally."""
    executed = False

    repo = _FakeRepository(claim_results=[_claimed(operation="ghost.operation")])
    worker = AsyncCommandWorker(repo, handlers={}, worker_id="w")
    result = await worker.run_once()

    assert result.rejected == 1
    assert executed is False
    assert len(repo.failed) == 1
    assert repo.failed[0][1] == "UNKNOWN_OPERATION"
    assert repo.completed == []
    assert repo.retried == []


async def test_unexpected_handler_crash_is_transient_and_scrubbed() -> None:
    async def handler(ctx: AsyncCommandContext) -> CommandSuccess:
        raise RuntimeError("secret PHI traceback")

    repo = _FakeRepository(claim_results=[_claimed(attempt_count=1)])
    worker = AsyncCommandWorker(repo, handlers={"session.advance": handler}, worker_id="w", max_attempts=8)
    result = await worker.run_once()

    assert result.retried == 1
    # No exception text is ever persisted into a retry/fail settle.
    assert repo.retried == [(repo.retried[0][0], repo.retried[0][1])]
    assert repo.failed == []


async def test_cancellation_propagates_and_never_settles() -> None:
    started = asyncio.Event()
    release = asyncio.Event()

    async def handler(ctx: AsyncCommandContext) -> CommandSuccess:
        started.set()
        await release.wait()
        return CommandSuccess(http_status=200, result_payload={})

    repo = _FakeRepository(claim_results=[_claimed()])
    worker = AsyncCommandWorker(repo, handlers={"session.advance": handler}, worker_id="w")

    task = asyncio.create_task(worker.run_once())
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    # A cancelled dispatch must never settle the command: the lease expires and
    # another worker reclaims it.
    assert repo.completed == []
    assert repo.failed == []
    assert repo.retried == []


async def test_complete_ownership_loss_maps_to_ownership_lost() -> None:
    async def handler(ctx: AsyncCommandContext) -> CommandSuccess:
        return CommandSuccess(http_status=200, result_payload={})

    repo = _FakeRepository(claim_results=[_claimed()])
    repo.complete_result = False
    worker = AsyncCommandWorker(repo, handlers={"session.advance": handler}, worker_id="w")
    result = await worker.run_once()

    assert result.ownership_lost == 1
    assert result.succeeded == 0


async def test_retry_ownership_loss_maps_to_ownership_lost() -> None:
    async def handler(ctx: AsyncCommandContext) -> CommandSuccess:
        raise CommandFailureError(error_code="TRANSIENT", retryable=True)

    repo = _FakeRepository(claim_results=[_claimed()])
    repo.retry_result = False
    worker = AsyncCommandWorker(repo, handlers={"session.advance": handler}, worker_id="w")
    result = await worker.run_once()

    assert result.ownership_lost == 1
    assert result.retried == 0


async def test_run_once_counts_a_mixed_batch() -> None:
    async def ok(ctx: AsyncCommandContext) -> CommandSuccess:
        return CommandSuccess(http_status=200, result_payload={})

    async def flaky(ctx: AsyncCommandContext) -> CommandSuccess:
        raise CommandFailureError(error_code="BUSY", retryable=True)

    repo = _FakeRepository(
        claim_results=[
            _claimed(operation="intake.message"),
            _claimed(operation="session.advance"),
            _claimed(operation="prescription.review"),
        ]
    )
    worker = AsyncCommandWorker(
        repo,
        handlers={"intake.message": ok, "session.advance": flaky},
        worker_id="w",
        max_attempts=8,
    )
    result = await worker.run_once()

    assert result.claimed == 3
    assert result.succeeded == 1
    assert result.retried == 1
    assert result.rejected == 1


async def test_same_session_commands_are_serialized() -> None:
    """阶段1：同一会话的命令必须串行处理（会话状态机依赖顺序推进）。"""
    active = 0
    max_active = 0

    async def handler(ctx: AsyncCommandContext) -> CommandSuccess:
        nonlocal active, max_active
        active += 1
        max_active = max(max_active, active)
        await asyncio.sleep(0.03)
        active -= 1
        return CommandSuccess(http_status=200, result_payload={})

    shared_session = uuid.uuid4()
    repo = _FakeRepository(
        claim_results=[
            _claimed(session_id=shared_session),
            _claimed(session_id=shared_session),
            _claimed(session_id=shared_session),
        ]
    )
    worker = AsyncCommandWorker(repo, handlers={"session.advance": handler}, worker_id="w", max_concurrency=8)
    result = await worker.run_once()

    assert max_active == 1, f"同会话命令不应并发，实际最大并发 {max_active}"
    assert result.succeeded == 3


async def test_different_session_commands_run_concurrently() -> None:
    """阶段1：不同会话的命令应真正并行，不再被串行 for 循环阻塞。"""
    active = 0
    max_active = 0

    async def handler(ctx: AsyncCommandContext) -> CommandSuccess:
        nonlocal active, max_active
        active += 1
        max_active = max(max_active, active)
        await asyncio.sleep(0.03)
        active -= 1
        return CommandSuccess(http_status=200, result_payload={})

    repo = _FakeRepository(claim_results=[_claimed() for _ in range(3)])
    worker = AsyncCommandWorker(repo, handlers={"session.advance": handler}, worker_id="w", max_concurrency=8)
    result = await worker.run_once()

    assert max_active >= 2, f"不同会话命令应并行，实际最大并发 {max_active}"
    assert result.succeeded == 3


async def test_max_concurrency_bounds_total_concurrency() -> None:
    """阶段1：全局信号量约束总并发，避免把模型网关/DB 打爆。"""
    active = 0
    max_active = 0

    async def handler(ctx: AsyncCommandContext) -> CommandSuccess:
        nonlocal active, max_active
        active += 1
        max_active = max(max_active, active)
        await asyncio.sleep(0.03)
        active -= 1
        return CommandSuccess(http_status=200, result_payload={})

    repo = _FakeRepository(claim_results=[_claimed() for _ in range(8)])
    worker = AsyncCommandWorker(repo, handlers={"session.advance": handler}, worker_id="w", max_concurrency=2)
    result = await worker.run_once()

    assert max_active <= 2, f"并发不应超过 max_concurrency=2，实际最大并发 {max_active}"
    assert result.succeeded == 8


def test_max_concurrency_validation() -> None:
    """阶段1：max_concurrency 必须在 [1, 32] 区间。"""
    with pytest.raises(ValueError):
        AsyncCommandWorker(_FakeRepository(), handlers={}, worker_id="w", max_concurrency=0)
    with pytest.raises(ValueError):
        AsyncCommandWorker(_FakeRepository(), handlers={}, worker_id="w", max_concurrency=33)


async def test_worker_registry_rejects_unplanned_operations() -> None:
    """The handler registry is validated against the finite operation allowlist."""
    async def ok(ctx: AsyncCommandContext) -> CommandSuccess:
        return CommandSuccess(http_status=200, result_payload={})

    with pytest.raises(ValueError):
        AsyncCommandWorker(
            _FakeRepository(),
            handlers={"doctor.prescribe": ok},
            worker_id="w",
        )


async def test_forced_cancel_does_not_hang_on_a_stuck_heartbeat() -> None:
    """A forced cancel must drain a stuck heartbeat without awaiting it.

    The fenced start hits the stuck renew before the handler can begin, so we
    cancel while the guard is blocked in the bounded renew attempt (which is
    itself capped by the attempt timeout) and assert prompt propagation.
    """
    class _HangingRepository(_FakeRepository):
        async def renew_lease(self, command_id, *, worker_id, lease_token, lease_seconds) -> bool:
            # A stuck DB call that never resolves.
            await asyncio.Event().wait()
            return True

    async def handler(ctx: AsyncCommandContext) -> CommandSuccess:
        return CommandSuccess(http_status=200, result_payload={})

    repo = _HangingRepository(claim_results=[_claimed()])
    worker = AsyncCommandWorker(repo, handlers={"session.advance": handler}, worker_id="w")
    task = asyncio.create_task(worker.run_once())
    await asyncio.sleep(0.05)  # let the fenced start reach the stuck renew attempt
    task.cancel()
    # Must propagate CancelledError promptly (within 2s), not hang on the stuck
    # heartbeat renew_lease. Without the drain fix this would time out.
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=2)
    # Cancelled dispatch never settles: the lease is left for reclaim.
    assert repo.completed == []
    assert repo.failed == []
    assert repo.retried == []


# ---------------------------------------------------------------------------
# R8-B shared lease guard integration (ownership-loss convergence)
# ---------------------------------------------------------------------------


def _guard_worker(repo: _FakeRepository, handler: Any) -> AsyncCommandWorker:
    """A worker with short, deterministic guard timings (heartbeat < lease).

    ``lease_seconds`` is clamped to the worker's minimum of 1 so deadline
    exhaustion tests stay short but deterministic and never flaky.
    """
    return AsyncCommandWorker(
        repo,
        handlers={"session.advance": handler},
        worker_id="w",
        heartbeat_interval_seconds=0.02,
        lease_seconds=1,
    )


async def test_preflight_renew_false_prevents_handler_and_never_settles() -> None:
    """A fenced start with known loss means the handler never starts."""
    started = asyncio.Event()

    async def handler(ctx: AsyncCommandContext) -> CommandSuccess:
        started.set()
        return CommandSuccess(http_status=200, result_payload={})

    async def renew(command_id, *, worker_id, lease_token, lease_seconds) -> bool:
        del command_id, worker_id, lease_token, lease_seconds
        return False  # known ownership loss before the handler may start

    repo = _FakeRepository(claim_results=[_claimed()], renew=renew)
    worker = _guard_worker(repo, handler)
    result = await worker.run_once()

    assert not started.is_set()  # handler never started
    assert result.ownership_lost == 1
    assert result.succeeded == 0
    assert repo.completed == []
    assert repo.failed == []
    assert repo.retried == []


async def test_renew_false_after_start_cancels_blocked_handler_and_never_settles() -> None:
    """Known loss after a confirmed start cancels a blocked handler."""
    started = asyncio.Event()
    calls = {"n": 0}

    async def handler(ctx: AsyncCommandContext) -> CommandSuccess:
        started.set()
        await asyncio.Event().wait()
        return CommandSuccess(http_status=200, result_payload={})

    async def renew(command_id, *, worker_id, lease_token, lease_seconds) -> bool:
        del command_id, worker_id, lease_token, lease_seconds
        calls["n"] += 1
        return calls["n"] == 1  # preflight True, next heartbeat False

    repo = _FakeRepository(claim_results=[_claimed()], renew=renew)
    worker = _guard_worker(repo, handler)
    result = await worker.run_once()

    assert started.is_set()  # handler was entered, then cancelled
    assert result.ownership_lost == 1
    assert result.succeeded == 0
    assert repo.completed == []
    assert repo.failed == []
    assert repo.retried == []


async def test_transient_renew_error_then_success_allows_completion() -> None:
    calls = {"n": 0}

    async def handler(ctx: AsyncCommandContext) -> CommandSuccess:
        del ctx
        await asyncio.sleep(0.06)  # outlive the transient failure heartbeat
        return CommandSuccess(http_status=200, result_payload={"ok": True})

    async def renew(command_id, *, worker_id, lease_token, lease_seconds) -> bool:
        del command_id, worker_id, lease_token, lease_seconds
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("transient DB hiccup")
        return True

    repo = _FakeRepository(claim_results=[_claimed()], renew=renew)
    worker = _guard_worker(repo, handler)
    result = await worker.run_once()

    assert result.succeeded == 1
    assert len(repo.completed) == 1
    assert calls["n"] >= 2


async def test_repeated_renew_errors_hit_deadline_and_never_settle() -> None:
    async def handler(ctx: AsyncCommandContext) -> CommandSuccess:
        await asyncio.Event().wait()
        return CommandSuccess(http_status=200, result_payload={})

    async def renew(command_id, *, worker_id, lease_token, lease_seconds) -> bool:
        del command_id, worker_id, lease_token, lease_seconds
        raise RuntimeError("permanently failing renew")

    repo = _FakeRepository(claim_results=[_claimed()], renew=renew)
    worker = _guard_worker(repo, handler)
    result = await worker.run_once()

    assert result.ownership_lost == 1
    assert result.succeeded == 0
    assert repo.completed == []
    assert repo.failed == []
    assert repo.retried == []


async def test_stuck_renew_is_bounded_fails_closed() -> None:
    async def handler(ctx: AsyncCommandContext) -> CommandSuccess:
        await asyncio.Event().wait()
        return CommandSuccess(http_status=200, result_payload={})

    async def renew(command_id, *, worker_id, lease_token, lease_seconds) -> bool:
        del command_id, worker_id, lease_token, lease_seconds
        await asyncio.Event().wait()  # stuck DB call never resolves
        return True

    repo = _FakeRepository(claim_results=[_claimed()], renew=renew)
    worker = _guard_worker(repo, handler)
    result = await worker.run_once()

    assert result.ownership_lost == 1
    assert repo.completed == []
    assert repo.failed == []
    assert repo.retried == []


async def test_external_cancellation_remains_prompt_under_guard() -> None:
    started = asyncio.Event()

    async def handler(ctx: AsyncCommandContext) -> CommandSuccess:
        started.set()
        await asyncio.Event().wait()
        return CommandSuccess(http_status=200, result_payload={})

    repo = _FakeRepository(claim_results=[_claimed()])  # healthy renew
    worker = _guard_worker(repo, handler)
    task = asyncio.create_task(worker.run_once())
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=1)
    assert repo.completed == []
    assert repo.failed == []
    assert repo.retried == []


async def test_preflight_true_then_final_false_is_fail_closed() -> None:
    """A fast completion with a failed final confirmation must not settle."""
    calls = {"n": 0}

    async def handler(ctx: AsyncCommandContext) -> CommandSuccess:
        del ctx
        return CommandSuccess(http_status=200, result_payload={"stale": True})

    async def renew(command_id, *, worker_id, lease_token, lease_seconds) -> bool:
        del command_id, worker_id, lease_token, lease_seconds
        calls["n"] += 1
        return calls["n"] == 1  # preflight True, final confirmation False

    repo = _FakeRepository(claim_results=[_claimed()], renew=renew)
    worker = _guard_worker(repo, handler)
    result = await worker.run_once()

    assert result.ownership_lost == 1
    assert result.succeeded == 0
    assert repo.completed == []
    assert repo.failed == []
    assert repo.retried == []
