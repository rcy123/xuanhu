"""R6-B regression: sync semantics of ``run_langgraph_advance_flow``.

This is the single shared business implementation used by both the synchronous
``POST /advance`` route and the R6-B async ``session.advance`` worker handler.
Its loop must preserve the exact legacy sync behavior:

- **Immediate terminal return**: as soon as a round yields a terminal outcome
  (review passed, or blocked / attempts exhausted), the flow returns *without*
  consuming any further rounds — a caller must never observe a response other
  than the round that settled the session.
- **Max safety auto-reopen**: when the safety review fails, the session is
  reset back to ``syndrome`` and a fresh reasoning round re-drafts the formula.
  The loop runs at most ``MAX_SAFETY_REOPEN_ATTEMPTS + 1`` rounds; each reopen
  after the first uses a fresh internal idempotency key
  ``{key}:safety-reopen:{attempt}`` so the durable-claim repair never replays a
  stale response.
- First round only carries the caller's ``state_version`` / ``alternative_index``
  (optimistic-concurrency check); subsequent rounds pass ``None`` so the
  internal reload sees the latest session state.

The business worker ``_run_langgraph_advance`` and the session loader are both
monkeypatched so the *loop* semantics are exercised deterministically with no
DB or model call.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from app.api.advance import run_langgraph_advance_flow
from app.services.langgraph_review import MAX_SAFETY_REOPEN_ATTEMPTS

_LANGGRAPH_SESSION = SimpleNamespace(agent_runtime="langgraph")


@pytest.fixture(autouse=True)
def _flow(monkeypatch: pytest.MonkeyPatch):
    """Route the loop's DB/model work to a scriptable fake."""
    import app.api.advance as adv_mod

    calls: list[dict[str, Any]] = []

    async def fake_load_session(_db: Any, session_id: str) -> Any:
        return _LANGGRAPH_SESSION

    async def fake_run_advance(db: Any, session: Any, **kwargs: Any) -> dict[str, Any]:
        del db, session
        calls.append(kwargs)
        return _pop_response()

    monkeypatch.setattr(adv_mod, "_load_session_for_advance", fake_load_session)
    monkeypatch.setattr(adv_mod, "_run_langgraph_advance", fake_run_advance)

    # The scripted responses are consumed in order.
    responses: list[dict[str, Any]] = []

    def _pop_response() -> dict[str, Any]:
        if not responses:
            return {"current_stage": "review"}  # safe terminal default
        return responses.pop(0)

    def _enqueue(response: dict[str, Any]) -> None:
        responses.append(response)

    return SimpleNamespace(calls=calls, enqueue=_enqueue)


async def _invoke(
    _flow: Any,
    *,
    idempotency_key: str = "caller-key",
    state_version: int | None = 7,
    alternative_index: int | None = 2,
) -> dict[str, Any]:
    return await run_langgraph_advance_flow(
        object(),  # db (unused once business worker is patched)
        session_id="00000000-0000-0000-0000-000000000001",
        state_version=state_version,
        trace_id="trace-1",
        force=False,
        idempotency_key=idempotency_key,
        alternative_index=alternative_index,
        shared_runtime=object(),
        allow_request_local_runtime=False,
    )


def _first_call(_flow: Any) -> dict[str, Any]:
    return _flow.calls[0]


async def test_terminal_review_returns_immediately_on_first_round(_flow: Any) -> None:
    _flow.enqueue({"current_stage": "review", "pending_review": True})

    result = await _invoke(_flow)

    # Terminal outcome on the first round: the flow returns that round's response
    # and never runs a second round.
    assert result["current_stage"] == "review"
    assert len(_flow.calls) == 1
    first = _first_call(_flow)
    # First round carries the caller key, optimistic state_version and alt index.
    assert first["idempotency_key"] == "caller-key"
    assert first["state_version"] == 7
    assert first["alternative_index"] == 2


async def test_safety_reopen_continues_to_next_round(_flow: Any) -> None:
    # Round 0: safety fails -> session reset to syndrome (reopened_for_safety).
    _flow.enqueue({"current_stage": "syndrome", "reopened_for_safety": True})
    # Round 1: review passes -> terminal.
    _flow.enqueue({"current_stage": "review", "pending_review": True})

    result = await _invoke(_flow)

    assert result["current_stage"] == "review"
    assert len(_flow.calls) == 2
    # Reopen round uses a fresh internal key, skips stale state_version/alt index.
    second = _flow.calls[1]
    assert second["idempotency_key"] == "caller-key:safety-reopen:1"
    assert second["state_version"] is None
    assert second["alternative_index"] is None


async def test_stage_safety_round_continues_loop(_flow: Any) -> None:
    # Round 0: formula just drafted to safety (current_stage == "safety"), no
    # reopen marker yet -> the loop must continue to let REVIEW run.
    _flow.enqueue({"current_stage": "safety"})
    # Round 1: review passes -> terminal.
    _flow.enqueue({"current_stage": "review", "pending_review": True})

    result = await _invoke(_flow)

    assert result["current_stage"] == "review"
    assert len(_flow.calls) == 2


async def test_reopen_loop_is_bounded_at_max_attempts(_flow: Any) -> None:
    # Every round reopens for safety; the loop must stop after
    # MAX_SAFETY_REOPEN_ATTEMPTS + 1 rounds and return the last round's response
    # (attempts exhausted -> treated as blocked/terminal).
    for _ in range(MAX_SAFETY_REOPEN_ATTEMPTS + 1):
        _flow.enqueue({"current_stage": "syndrome", "reopened_for_safety": True})

    result = await _invoke(_flow)

    assert len(_flow.calls) == MAX_SAFETY_REOPEN_ATTEMPTS + 1
    assert result["reopened_for_safety"] is True
    # Reopen keys are distinct per round and never collide with the caller key.
    keys = [c["idempotency_key"] for c in _flow.calls]
    assert len(set(keys)) == len(keys) == MAX_SAFETY_REOPEN_ATTEMPTS + 1
    assert keys[0] == "caller-key"
    for attempt in range(1, MAX_SAFETY_REOPEN_ATTEMPTS + 1):
        assert keys[attempt] == f"caller-key:safety-reopen:{attempt}"
        # Subsequent rounds skip the optimistic-concurrency check.
        assert _flow.calls[attempt]["state_version"] is None
        assert _flow.calls[attempt]["alternative_index"] is None


async def test_legacy_session_is_rejected_before_any_round(
    _flow: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.api.advance as adv_mod
    from app.core.exceptions import AgentTriggerFailedError

    async def fake_legacy_load(_db: Any, session_id: str) -> Any:
        del _db, session_id
        return SimpleNamespace(agent_runtime="legacy")

    monkeypatch.setattr(adv_mod, "_load_session_for_advance", fake_legacy_load)

    with pytest.raises(AgentTriggerFailedError) as exc_info:
        await run_langgraph_advance_flow(
            object(),
            session_id="00000000-0000-0000-0000-000000000002",
            state_version=None,
            trace_id="trace-2",
            force=False,
            idempotency_key="caller-key",
            alternative_index=None,
            shared_runtime=object(),
            allow_request_local_runtime=False,
        )

    assert exc_info.value.agent_error_code == "LEGACY_RUNTIME_DECOMMISSIONED"
    # No business round was attempted for a decommissioned session.
    assert len(_flow.calls) == 0
