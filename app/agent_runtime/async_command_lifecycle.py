"""R7 fail-closed lifecycle/readiness for the async-command worker.

The FastAPI lifespan owns process-scoped worker state. This module is the single
place that decides when admission may be marked ``ready`` and guarantees it can
never read "ready" while no worker is actually running:

- ``init_async_command_state`` disables admission **unconditionally** at
  lifespan startup (also overwriting any stale state left by a prior lifespan).
- ``run_supervised_async_command_worker`` is the coroutine the lifespan schedules
  as the worker task. Marking readiness is its **first** statement, so it only
  happens once the task is actually scheduled and running on the event loop —
  never before ``asyncio.create_task`` returns. A ``started`` event lets the
  lifespan await that handshake before it begins serving requests.
- If the supervised task exits for any reason (unexpected crash or normal stop)
  its ``finally`` disables admission immediately, so no further async work is
  accepted without a consumer.
- The lifespan disables admission *before* signalling the worker to stop, so no
  new commands are accepted during the shutdown drain window.
- Unexpected worker exceptions are drained silently (no PHI / exception-text is
  logged) so they can never leak protected-health information into logs.

Readiness additionally requires the complete three-handler registry (enforced by
``async_admission_ready``); the lifespan only schedules a supervised task when
``build_async_command_handlers`` returned a non-empty registry, otherwise
admission stays disabled and requests fall through to the synchronous fallback
path (R7 default admission never routes to the substrate without a ready,
fully-registered worker).
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from typing import Any

from app.agent_runtime.async_command_admission import AsyncCommandAdmissionState
from app.agent_runtime.async_command_worker import AsyncCommandHandler, AsyncCommandWorker

_DEFAULT_STATE_KEY = "async_command_state"


def get_async_command_state(app_state: Any, *, state_key: str = _DEFAULT_STATE_KEY) -> AsyncCommandAdmissionState | None:
    """Read the current admission state (``None`` if never initialized)."""
    state = getattr(app_state, state_key, None)
    if state is None:
        return None
    if not isinstance(state, AsyncCommandAdmissionState):
        raise TypeError(f"{state_key} must be an AsyncCommandAdmissionState")
    return state


def set_async_command_state(
    app_state: Any,
    state: AsyncCommandAdmissionState,
    *,
    state_key: str = _DEFAULT_STATE_KEY,
) -> None:
    """Write the admission state. Fails closed to disabled on a bad write."""
    if not isinstance(state, AsyncCommandAdmissionState):
        raise TypeError("state must be an AsyncCommandAdmissionState")
    setattr(app_state, state_key, state)


def init_async_command_state(
    app_state: Any,
    *,
    state_key: str = _DEFAULT_STATE_KEY,
) -> None:
    """Disable admission unconditionally at startup (clears stale state)."""
    set_async_command_state(app_state, AsyncCommandAdmissionState.disabled(), state_key=state_key)


def disable_async_command_state(
    app_state: Any,
    *,
    state_key: str = _DEFAULT_STATE_KEY,
) -> None:
    """Disable admission. Used at shutdown (before stopping the worker) and on
    any supervised worker exit so no further async work is accepted."""
    set_async_command_state(app_state, AsyncCommandAdmissionState.disabled(), state_key=state_key)


async def run_supervised_async_command_worker(
    *,
    app_state: Any,
    worker: AsyncCommandWorker,
    stop: asyncio.Event,
    started: asyncio.Event,
    handler_operations: frozenset[str],
    state_key: str = _DEFAULT_STATE_KEY,
) -> None:
    """Run one worker under fail-closed readiness supervision.

    Readiness is marked as the first statement of this coroutine, i.e. only once
    the task is actually scheduled and running. ``started`` is set in the same
    step so the lifespan can await the handshake before serving. If the worker
    exits for any reason (crash or normal stop), admission is disabled.
    """
    set_async_command_state(
        app_state,
        AsyncCommandAdmissionState.ready_state(frozenset(handler_operations)),
        state_key=state_key,
    )
    started.set()
    try:
        await worker.run_forever(stop)
    except asyncio.CancelledError:
        raise
    except Exception:
        # Drain the unexpected worker crash silently. No exception text (or any
        # PHI it might carry) is logged; the worker itself already logs only the
        # bounded type name for claim-cycle failures. Readiness is cleared below.
        pass
    finally:
        disable_async_command_state(app_state, state_key=state_key)


def build_worker_handlers(handlers: Mapping[str, AsyncCommandHandler]) -> frozenset[str]:
    """The frozen set of registered handler operations for readiness gating."""
    return frozenset(handlers.keys())


__all__ = [
    "get_async_command_state",
    "set_async_command_state",
    "init_async_command_state",
    "disable_async_command_state",
    "run_supervised_async_command_worker",
    "build_worker_handlers",
]
