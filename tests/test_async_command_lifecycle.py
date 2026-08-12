"""R6-B fail-closed async-command lifecycle/readiness tests.

These are deterministic (no DB, no real runtime) tests for the race-safe
lifecycle contract:

- Admission is initialized disabled unconditionally (also clearing stale state).
- Readiness is marked ONLY by the supervised worker task once it is actually
  scheduled/running (start handshake) — never before ``asyncio.create_task``
  returns, and never by the lifespan itself.
- If the supervised task exits for any reason (crash or normal stop) admission
  is immediately disabled again.
- On lifespan shutdown admission is disabled BEFORE the worker is stopped.
- Unexpected worker exceptions are drained without any PHI / exception-text
  being logged.

One lifespan-level test exercises the ``app.main`` wiring (enabled -> ready via
handshake -> disabled before shutdown), while the supervisor tests cover the
state machine directly with fake workers.
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import FastAPI

from app.agent_runtime.async_command_admission import (
    ALLOWED_ADMISSION_OPERATIONS,
    async_admission_ready,
)
from app.agent_runtime.async_command_lifecycle import (
    disable_async_command_state,
    get_async_command_state,
    init_async_command_state,
    run_supervised_async_command_worker,
)
from app.agent_runtime.async_command_worker import AsyncCommandWorker

OPS = frozenset(ALLOWED_ADMISSION_OPERATIONS)


class _NullRepository:
    """Repository stand-in: the lifecycle fakes override ``run_forever`` and
    never touch the repository, so it only needs to be constructible."""


class _BlockingWorker(AsyncCommandWorker):
    """run_forever blocks until stop is set; never crashes."""

    def __init__(self) -> None:
        super().__init__(_NullRepository(), worker_id="lifecycle-blocking", handlers={})
        self.exited = False

    async def run_forever(self, stop: asyncio.Event) -> None:  # type: ignore[override]
        await stop.wait()
        self.exited = True


class _CrashingWorker(AsyncCommandWorker):
    """run_forever crashes immediately with a PHI-bearing exception."""

    def __init__(self) -> None:
        super().__init__(_NullRepository(), worker_id="lifecycle-crash", handlers={})
        self.exited = False

    async def run_forever(self, stop: asyncio.Event) -> None:  # type: ignore[override]
        del stop
        self.exited = True
        raise RuntimeError("boom PHI-7f3e must-not-leak")


def _app_state() -> SimpleNamespace:
    return SimpleNamespace()


# ---------------------------------------------------------------------------
# supervisor state machine (deterministic, no DB)
# ---------------------------------------------------------------------------


async def test_init_disables_unconditionally_and_overwrites_stale() -> None:
    app_state = _app_state()
    # A stale "ready" state from a prior lifespan must be cleared.
    from app.agent_runtime.async_command_admission import AsyncCommandAdmissionState

    app_state.async_command_state = AsyncCommandAdmissionState.ready_state(OPS)
    init_async_command_state(app_state)
    state = get_async_command_state(app_state)
    assert state is not None
    assert state.enabled is False
    assert state.ready is False
    assert async_admission_ready(state) is False


async def test_ready_only_after_start_handshake() -> None:
    app_state = _app_state()
    init_async_command_state(app_state)
    stop = asyncio.Event()
    started = asyncio.Event()
    worker = _BlockingWorker()
    task = asyncio.create_task(
        run_supervised_async_command_worker(
            app_state=app_state,
            worker=worker,
            stop=stop,
            started=started,
            handler_operations=OPS,
        )
    )
    try:
        # Immediately after create_task (before the task has run) admission must
        # NOT be ready — the lifespan has not done a handshake yet.
        assert async_admission_ready(get_async_command_state(app_state)) is False
        # Handshake: once the task actually runs it marks readiness itself.
        await asyncio.wait_for(started.wait(), timeout=5)
        state = get_async_command_state(app_state)
        assert state is not None
        assert state.ready is True
        assert state.handler_operations == OPS
        assert async_admission_ready(state) is True
    finally:
        stop.set()
        await asyncio.wait_for(task, timeout=5)


async def test_stop_disables_on_normal_exit() -> None:
    app_state = _app_state()
    init_async_command_state(app_state)
    stop = asyncio.Event()
    started = asyncio.Event()
    worker = _BlockingWorker()
    task = asyncio.create_task(
        run_supervised_async_command_worker(
            app_state=app_state,
            worker=worker,
            stop=stop,
            started=started,
            handler_operations=OPS,
        )
    )
    await asyncio.wait_for(started.wait(), timeout=5)
    assert get_async_command_state(app_state).ready is True
    stop.set()
    await asyncio.wait_for(task, timeout=5)
    assert worker.exited is True
    state = get_async_command_state(app_state)
    assert state is not None
    assert state.ready is False
    assert state.enabled is False
    assert async_admission_ready(state) is False


async def test_unexpected_crash_clears_readiness_without_leaking(
    caplog: pytest.LogCaptureFixture,
) -> None:
    app_state = _app_state()
    init_async_command_state(app_state)
    stop = asyncio.Event()
    started = asyncio.Event()
    worker = _CrashingWorker()
    task = asyncio.create_task(
        run_supervised_async_command_worker(
            app_state=app_state,
            worker=worker,
            stop=stop,
            started=started,
            handler_operations=OPS,
        )
    )
    with caplog.at_level(logging.WARNING):
        # The crash is drained silently; awaiting the task does not raise and the
        # exception text/PHI never reaches any log.
        await asyncio.wait_for(task, timeout=5)
    assert worker.exited is True
    assert "boom" not in caplog.text
    assert "PHI-7f3e" not in caplog.text
    state = get_async_command_state(app_state)
    assert state is not None
    assert state.ready is False
    assert async_admission_ready(state) is False


async def test_ready_requires_full_registry_via_supervisor() -> None:
    # A supervisor started with only a partial registry must not mark ready.
    app_state = _app_state()
    init_async_command_state(app_state)
    stop = asyncio.Event()
    started = asyncio.Event()
    worker = _BlockingWorker()
    task = asyncio.create_task(
        run_supervised_async_command_worker(
            app_state=app_state,
            worker=worker,
            stop=stop,
            started=started,
            handler_operations=frozenset({"intake.message"}),
        )
    )
    try:
        await asyncio.wait_for(started.wait(), timeout=5)
        # The registry does not cover all allowlisted operations => fail closed.
        assert async_admission_ready(get_async_command_state(app_state)) is False
    finally:
        stop.set()
        await asyncio.wait_for(task, timeout=5)


async def test_disable_helper_fails_closed() -> None:
    app_state = _app_state()
    init_async_command_state(app_state)
    disable_async_command_state(app_state)
    state = get_async_command_state(app_state)
    assert state is not None
    assert state.enabled is False
    assert async_admission_ready(state) is False


# ---------------------------------------------------------------------------
# lifespan wiring (app.main)
# ---------------------------------------------------------------------------


def _settings(async_command_enabled: bool) -> SimpleNamespace:
    return SimpleNamespace(
        app_env="local",
        xuanhu_prod_secret_guard=True,
        database_url="postgresql://must-not-be-logged",
        outbox_publisher_enabled=False,
        outbox_publisher_shutdown_grace_seconds=1.0,
        async_command_enabled=async_command_enabled,
        async_command_shutdown_grace_seconds=5.0,
        async_command_batch_size=10,
        async_command_lease_seconds=60,
        async_command_heartbeat_seconds=20,
        async_command_max_attempts=8,
        async_command_retry_base_seconds=1,
        async_command_retry_max_seconds=300,
        async_command_poll_interval_seconds=0.5,
        safe_dump=lambda: {"database_url": "***"},
    )


class _RecordingWorker:
    """Records the real ``app.state`` admission state the moment it stops.

    ``holder["app_state"]`` is populated once the lifespan has entered and the
    real ``application.state`` is known, before shutdown triggers ``stop``.
    """

    def __init__(self, holder: dict[str, Any]) -> None:
        self._holder = holder
        self.state_at_exit: Any = None

    async def run_forever(self, stop: asyncio.Event) -> None:  # type: ignore[no-untyped-def]
        await stop.wait()
        app_state = self._holder.get("app_state")
        assert app_state is not None
        # Record the admission state the instant the worker observes shutdown.
        self.state_at_exit = get_async_command_state(app_state)


async def test_lifespan_disabled_startup_sets_disabled_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app import main as main_module

    app_state: Any = None

    @asynccontextmanager
    async def fake_shared_runtime(_db_url: str):  # type: ignore[no-untyped-def]
        yield _minimal_runtime()

    monkeypatch.setattr(main_module, "get_settings", lambda: _settings(False))
    monkeypatch.setattr(main_module, "shared_langgraph_runtime", fake_shared_runtime)
    application = FastAPI()

    async with main_module.lifespan(application):
        app_state = get_async_command_state(application.state)

    # Feature disabled => admission is present and disabled (never absent/stale).
    assert app_state is not None
    assert app_state.enabled is False
    assert app_state.ready is False
    assert async_admission_ready(app_state) is False


async def test_lifespan_ready_via_handshake_and_disables_before_shutdown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app import main as main_module

    @asynccontextmanager
    async def fake_shared_runtime(_db_url: str):  # type: ignore[no-untyped-def]
        yield _minimal_runtime()

    monkeypatch.setattr(main_module, "get_settings", lambda: _settings(True))
    monkeypatch.setattr(main_module, "shared_langgraph_runtime", fake_shared_runtime)
    # These are imported *locally* inside the lifespan function, so they must be
    # patched on their source modules (not on ``main``).
    import app.agent_runtime.async_command_worker as _worker_mod
    import app.agent_runtime.async_handlers as _handlers_mod

    monkeypatch.setattr(
        _handlers_mod,
        "build_async_command_handlers",
        lambda _runtime: {op: _noop_handler for op in OPS},
    )

    app_state_holder: dict[str, Any] = {}
    worker = _RecordingWorker(app_state_holder)

    monkeypatch.setattr(
        _worker_mod,
        "build_async_command_worker",
        lambda *_a, **_k: worker,
    )
    application = FastAPI()

    async with main_module.lifespan(application):
        # Give the recording worker a handle on the real app state now that the
        # lifespan is serving, before shutdown triggers the stop event.
        app_state_holder["app_state"] = application.state
        # After the start handshake admission is ready.
        assert async_admission_ready(get_async_command_state(application.state)) is True

    # Shutdown: by the time the worker observed the stop, admission was already
    # disabled (the lifespan disables BEFORE stopping/cancelling the worker).
    assert worker.state_at_exit is not None
    assert worker.state_at_exit.enabled is False
    assert worker.state_at_exit.ready is False
    # Final state disabled.
    assert async_admission_ready(get_async_command_state(application.state)) is False


async def _noop_handler(*_args: Any, **_kwargs: Any) -> Any:  # pragma: no cover
    raise AssertionError("noop handler must never be dispatched in lifecycle tests")


def _minimal_runtime():  # type: ignore[no-untyped-def]
    from tests.test_l1_application_lifecycle import _runtime

    return _runtime()
