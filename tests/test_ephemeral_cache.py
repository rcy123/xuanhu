from __future__ import annotations

import uuid
from types import SimpleNamespace
from typing import Any

import pytest

from app.agent_runtime.ephemeral_cache import BoundedTTLCache
from app.services import langgraph_intake as intake_module
from app.services import langgraph_reasoning as reasoning_module


def test_cache_evicts_lru_entries_at_hard_limit() -> None:
    cache: BoundedTTLCache[int, str] = BoundedTTLCache(max_size=2, ttl_seconds=60)
    cache[1] = "one"
    cache[2] = "two"
    assert cache[1] == "one"

    cache[3] = "three"

    assert 1 in cache
    assert 2 not in cache
    assert cache[3] == "three"
    assert len(cache) == 2


def test_cache_expires_entries(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = iter((10.0, 10.5, 11.1))
    monkeypatch.setattr("app.agent_runtime.ephemeral_cache.time.monotonic", lambda: next(clock))
    cache: BoundedTTLCache[str, str] = BoundedTTLCache(max_size=2, ttl_seconds=1)

    cache["key"] = "value"

    assert cache.get("key") == "value"
    assert len(cache) == 0


def test_ten_thousand_claims_cannot_grow_cache_unbounded() -> None:
    cache: BoundedTTLCache[uuid.UUID, str] = BoundedTTLCache(max_size=256, ttl_seconds=300)

    for index in range(10_000):
        cache[uuid.UUID(int=index)] = str(index)

    assert len(cache) == cache.max_size == 256


def test_actual_runtime_caches_remain_bounded_after_ten_thousand_claims() -> None:
    caches: tuple[tuple[Any, Any], ...] = (
        (intake_module._INTAKE_OUTPUT_CACHE, object()),
        (reasoning_module._SYNDROME_RESULT_CACHE, object()),
        (reasoning_module._FORMULA_ROUTE_CACHE, "manual_required"),
    )
    try:
        for cache, value in caches:
            cache.clear()
            for index in range(10_000):
                cache[uuid.UUID(int=index)] = value
            assert len(cache) == cache.max_size == 256
    finally:
        for cache, _ in caches:
            cache.clear()


class _FakeAsyncSession:
    def __init__(self, claim: Any) -> None:
        self.claim = claim

    async def __aenter__(self) -> _FakeAsyncSession:
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None

    def in_transaction(self) -> bool:
        return False

    async def rollback(self) -> None:
        return None

    def begin(self) -> _FakeAsyncSession:
        return self

    async def get(self, model: object, _claim_id: uuid.UUID, **_kwargs: object) -> Any:
        # 0d-2: _mark_claim_failed 会查询 ConsultSession 以写入 recovery_status。
        if getattr(model, "__name__", None) == "ConsultSession":
            return SimpleNamespace(status="active", recovery_status="normal", updated_at=None)
        return self.claim


@pytest.mark.asyncio
async def test_failed_intake_claim_releases_ephemeral_output() -> None:
    claim_id = uuid.uuid4()
    claim = SimpleNamespace(
        status="running",
        error_code=None,
        updated_at=None,
        run_id=uuid.uuid4(),
        session_id=uuid.uuid4(),
    )
    db = _FakeAsyncSession(claim)
    intake_module._INTAKE_OUTPUT_CACHE[claim_id] = object()  # type: ignore[assignment]

    runner = intake_module.LangGraphIntakeMessageRunner(db)  # type: ignore[arg-type]
    await runner._mark_claim_failed(claim_id, "MODEL_FAILED")  # noqa: SLF001

    assert claim.status == "failed"
    assert claim_id not in intake_module._INTAKE_OUTPUT_CACHE


@pytest.mark.asyncio
async def test_failed_reasoning_claim_releases_all_ephemeral_values(monkeypatch: pytest.MonkeyPatch) -> None:
    claim_id = uuid.uuid4()
    claim = SimpleNamespace(status="running", error_code=None, updated_at=None)
    db = _FakeAsyncSession(claim)
    monkeypatch.setattr(reasoning_module, "get_session_factory", lambda: lambda: db)
    reasoning_module._SYNDROME_RESULT_CACHE[claim_id] = object()  # type: ignore[assignment]
    reasoning_module._FORMULA_ROUTE_CACHE[claim_id] = "manual_required"

    await reasoning_module._mark_claim_failed(claim_id, "MODEL_FAILED")  # noqa: SLF001

    assert claim.status == "failed"
    assert claim_id not in reasoning_module._SYNDROME_RESULT_CACHE
    assert claim_id not in reasoning_module._FORMULA_ROUTE_CACHE


@pytest.mark.asyncio
async def test_completed_reasoning_claim_releases_all_ephemeral_values() -> None:
    claim_id = uuid.uuid4()
    reasoning_module._SYNDROME_RESULT_CACHE[claim_id] = object()  # type: ignore[assignment]
    reasoning_module._FORMULA_ROUTE_CACHE[claim_id] = "manual_required"
    claim = type(
        "Claim",
        (),
        {"id": claim_id, "status": "completed", "response_payload": {"route": "reasoning_subgraph_v1"}},
    )()

    result = await reasoning_module._completed_graph_update(claim)

    assert result is not None
    assert claim_id not in reasoning_module._SYNDROME_RESULT_CACHE
    assert claim_id not in reasoning_module._FORMULA_ROUTE_CACHE
