"""PostgreSQL acceptance tests for durable public HTTP command idempotency."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import subprocess
import sys
import time
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
from fastapi import Request
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.api.request_context import WriteRequestContext, write_request_context
from app.core.exceptions import (
    HttpCommandRecoveryRequiredError,
    HttpCommandReplayError,
    IdempotencyConflictError,
    SessionBusyError,
    ValidationError,
)
from app.db.session import get_session_factory
from app.main import app
from app.models.consult import ConsultSession
from app.models.http_command import HttpCommandClaim
from app.services.http_idempotency import HttpCommandExecutor

pytestmark = [pytest.mark.integration, pytest.mark.asyncio(loop_scope="module")]

_OPERATION_PREFIX = "p1-02-acceptance."
_PATIENT_REF_PREFIX = "P1-02-IDEMP-"
_PUBLIC_CLAIM_DIGESTS: set[str] = set()
_PROCESS_WORKER_CONFIG_ENV = "XUANHU_IDEMPOTENCY_PROCESS_CONFIG"


@pytest_asyncio.fixture(loop_scope="module")
async def factory() -> async_sessionmaker[AsyncSession]:
    return get_session_factory()


@pytest_asyncio.fixture(loop_scope="module", autouse=True)
async def cleanup(factory: async_sessionmaker[AsyncSession]) -> None:
    yield
    async with factory() as db, db.begin():
        await db.execute(
            delete(HttpCommandClaim).where(
                (HttpCommandClaim.operation.like(f"{_OPERATION_PREFIX}%"))
                | (HttpCommandClaim.idempotency_key_digest.in_(_PUBLIC_CLAIM_DIGESTS))
            )
        )
        await db.execute(
            delete(ConsultSession).where(
                ConsultSession.patient_ref.like(f"{_PATIENT_REF_PREFIX}%")
            )
        )


async def _execute(
    factory: async_sessionmaker[AsyncSession],
    *,
    operation: str,
    scope: str | None,
    key: str,
    payload: dict[str, Any],
    handler: Any,
    is_idempotent: bool = True,
) -> Any:
    async with factory() as db:
        return await HttpCommandExecutor(db, session_factory=factory).execute(
            operation=operation,
            scope_key=scope or "global",
            concurrency_scope=scope,
            idempotency_key=key,
            is_idempotent=is_idempotent,
            request_payload=payload,
            success_status=200,
            success_message="ok",
            handler=lambda: handler(db),
        )


def _headerless_context() -> WriteRequestContext:
    return write_request_context(
        Request(
            {
                "type": "http",
                "http_version": "1.1",
                "method": "POST",
                "scheme": "http",
                "path": "/",
                "raw_path": b"/",
                "query_string": b"",
                "headers": [],
                "client": ("127.0.0.1", 1),
                "server": ("test", 80),
            }
        )
    )


async def test_same_key_concurrent_workers_execute_one_side_effect_and_replay(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    operation = f"{_OPERATION_PREFIX}concurrent"
    session_id = uuid.uuid4()
    scope = f"session:{session_id}"
    calls = 0

    async def handler(db: AsyncSession) -> dict[str, Any]:
        nonlocal calls
        calls += 1
        await asyncio.sleep(0.1)
        db.add(
            ConsultSession(
                id=session_id,
                patient_ref=f"{_PATIENT_REF_PREFIX}{session_id}",
                patient_info={},
                state_version=1,
            )
        )
        return {"session_id": str(session_id), "attempt": 1}

    first, second = await asyncio.gather(
        _execute(
            factory,
            operation=operation,
            scope=scope,
            key="same-public-key",
            payload={"body": {"value": 1}},
            handler=handler,
        ),
        _execute(
            factory,
            operation=operation,
            scope=scope,
            key="same-public-key",
            payload={"body": {"value": 1}},
            handler=handler,
        ),
    )

    assert first.data == second.data
    assert {first.replayed, second.replayed} == {False, True}
    assert calls == 1
    async with factory() as db:
        assert await db.scalar(
            select(func.count()).select_from(ConsultSession).where(ConsultSession.id == session_id)
        ) == 1
        claim = await db.scalar(
            select(HttpCommandClaim).where(HttpCommandClaim.operation == operation)
        )
    assert claim is not None
    assert claim.status == "completed"
    assert claim.idempotency_mode == "public"


def _run_process_pair(
    *,
    tmp_path: Path,
    operation: str,
    scope: str,
    public_key: str,
    session_id: uuid.UUID,
    patient_ref: str,
    effect_token: str,
) -> list[dict[str, Any]]:
    """Run two credential-safe worker processes behind a shared start barrier."""

    helper = Path(__file__).with_name("_http_command_idempotency_subprocess.py")
    start_file = tmp_path / "start"
    ready_files = [tmp_path / f"worker-{index}.ready" for index in range(2)]
    entered_files = [tmp_path / f"worker-{index}.entered" for index in range(2)]
    processes: list[subprocess.Popen[str]] = []
    for index in range(2):
        config = {
            "label": f"worker-{index}",
            "operation": operation,
            "scope": scope,
            "idempotency_key": public_key,
            "session_id": str(session_id),
            "patient_ref": patient_ref,
            "effect_token": effect_token,
            "ready_file": str(ready_files[index]),
            "start_file": str(start_file),
            "entered_file": str(entered_files[index]),
            "peer_entered_files": [str(path) for path in entered_files],
        }
        env = {
            **os.environ,
            # Pass the already-provisioned worker database explicitly.  The
            # child validates TEST_DATABASE_URL + sentinel, then derives DB_URL
            # itself instead of trusting an inherited application target.
            "TEST_DATABASE_URL": os.environ["TEST_DATABASE_URL"],
            "XUANHU_ALLOW_DESTRUCTIVE_TESTS": "1",
            _PROCESS_WORKER_CONFIG_ENV: json.dumps(config, ensure_ascii=True),
        }
        env.pop("DB_URL", None)
        processes.append(
            subprocess.Popen(
                [sys.executable, str(helper)],
                cwd=Path(__file__).resolve().parents[1],
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
            )
        )

    try:
        ready_deadline = time.monotonic() + 30
        while not all(path.is_file() for path in ready_files):
            exited = [process.returncode for process in processes if process.poll() is not None]
            if exited:
                raise AssertionError(f"idempotency process exited before barrier: {exited}")
            if time.monotonic() >= ready_deadline:
                raise TimeoutError("idempotency process ready barrier timed out")
            time.sleep(0.01)
        start_file.touch()

        outputs: list[dict[str, Any]] = []
        for process in processes:
            stdout, stderr = process.communicate(timeout=60)
            assert process.returncode == 0, (
                f"idempotency worker failed with code {process.returncode}; "
                f"stdout={stdout!r}; stderr={stderr!r}"
            )
            parsed = json.loads(stdout)
            assert isinstance(parsed, dict)
            outputs.append(parsed)
        return outputs
    finally:
        for process in processes:
            if process.poll() is None:
                process.kill()
                process.communicate(timeout=10)


async def test_same_public_key_in_two_os_processes_commits_once_and_replays(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """Independent processes share one durable result and one business write."""

    session_id = uuid.uuid4()
    operation = f"{_OPERATION_PREFIX}os-process-{uuid.uuid4()}"
    scope = f"session:{session_id}"
    public_key = f"os-process-public-key-{uuid.uuid4()}"
    patient_ref = f"{_PATIENT_REF_PREFIX}{session_id}"
    effect_token = f"effect-{uuid.uuid4()}"

    outputs = await asyncio.to_thread(
        _run_process_pair,
        tmp_path=tmp_path,
        operation=operation,
        scope=scope,
        public_key=public_key,
        session_id=session_id,
        patient_ref=patient_ref,
        effect_token=effect_token,
    )

    assert {output["status"] for output in outputs} == {"ok"}
    assert {output["label"] for output in outputs} == {"worker-0", "worker-1"}
    worker_pids = {output["worker_pid"] for output in outputs}
    assert len(worker_pids) == 2
    assert os.getpid() not in worker_pids
    assert {output["http_status"] for output in outputs} == {201}
    assert {output["message"] for output in outputs} == {"created"}
    assert {output["replayed"] for output in outputs} == {False, True}
    assert outputs[0]["data"] == outputs[1]["data"]
    assert outputs[0]["data"]["session_id"] == str(session_id)
    assert outputs[0]["data"]["effect_token"] == effect_token
    assert outputs[0]["data"]["executed_by_pid"] in worker_pids
    assert sum(
        output["worker_pid"] == output["data"]["executed_by_pid"] for output in outputs
    ) == 1

    key_digest = hashlib.sha256(public_key.encode()).hexdigest()
    async with factory() as db:
        session_count = await db.scalar(
            select(func.count()).select_from(ConsultSession).where(ConsultSession.id == session_id)
        )
        claim_count = await db.scalar(
            select(func.count())
            .select_from(HttpCommandClaim)
            .where(
                HttpCommandClaim.operation == operation,
                HttpCommandClaim.scope_key == scope,
                HttpCommandClaim.idempotency_key_digest == key_digest,
            )
        )
        claim = await db.scalar(
            select(HttpCommandClaim).where(
                HttpCommandClaim.operation == operation,
                HttpCommandClaim.scope_key == scope,
                HttpCommandClaim.idempotency_key_digest == key_digest,
            )
        )
    assert session_count == 1
    assert claim_count == 1
    assert claim is not None
    assert claim.status == "completed"
    assert claim.response_payload == {"data": outputs[0]["data"], "message": "created"}


async def test_completed_command_replays_after_disconnect_with_different_attempt(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    operation = f"{_OPERATION_PREFIX}disconnect"
    calls = 0

    async def handler(db: AsyncSession) -> dict[str, Any]:
        nonlocal calls
        del db
        calls += 1
        return {"stable": True}

    first = await _execute(
        factory,
        operation=operation,
        scope=None,
        key="network-retry-key",
        payload={"body": {"stable": True}},
        handler=handler,
    )
    second = await _execute(
        factory,
        operation=operation,
        scope=None,
        key="network-retry-key",
        payload={"body": {"stable": True}},
        handler=handler,
    )

    assert first.data == second.data == {"stable": True}
    assert not first.replayed and second.replayed
    assert calls == 1


async def test_same_key_different_request_digest_is_stable_conflict(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    operation = f"{_OPERATION_PREFIX}digest-conflict"

    async def handler(db: AsyncSession) -> dict[str, Any]:
        del db
        return {"ok": True}

    await _execute(
        factory,
        operation=operation,
        scope=None,
        key="digest-key",
        payload={"body": {"value": 1}},
        handler=handler,
    )
    with pytest.raises(IdempotencyConflictError) as captured:
        await _execute(
            factory,
            operation=operation,
            scope=None,
            key="digest-key",
            payload={"body": {"value": 2}},
            handler=handler,
        )
    assert captured.value.status_code == 409
    assert not captured.value.retryable


async def test_persisted_business_error_is_replayed_without_reexecution(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    operation = f"{_OPERATION_PREFIX}error-replay"
    calls = 0

    async def handler(db: AsyncSession) -> dict[str, Any]:
        nonlocal calls
        del db
        calls += 1
        raise ValidationError(message="fixed failure", detail="safe detail", retryable=False)

    with pytest.raises(ValidationError):
        await _execute(
            factory,
            operation=operation,
            scope=None,
            key="failed-key",
            payload={"body": {}},
            handler=handler,
        )
    with pytest.raises(HttpCommandReplayError) as replayed:
        await _execute(
            factory,
            operation=operation,
            scope=None,
            key="failed-key",
            payload={"body": {}},
            handler=handler,
        )
    assert replayed.value.code == "VALIDATION_ERROR"
    assert replayed.value.status_code == 422
    assert replayed.value.message == "fixed failure"
    assert calls == 1


async def test_expired_owner_becomes_ambiguous_and_never_reexecutes(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    operation = f"{_OPERATION_PREFIX}ambiguous"
    key = "expired-key"
    payload = {"body": {"value": 1}}
    key_digest = hashlib.sha256(key.encode()).hexdigest()
    request_digest = hashlib.sha256(
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode()
    ).hexdigest()
    claim_id = uuid.uuid4()
    async with factory() as db, db.begin():
        db.add(
            HttpCommandClaim(
                id=claim_id,
                operation=operation,
                scope_key="global",
                concurrency_scope=None,
                idempotency_mode="public",
                idempotency_key_digest=key_digest,
                request_digest=request_digest,
                status="running",
                owner_token=uuid.uuid4(),
                lease_expires_at=datetime.now(UTC) - timedelta(seconds=1),
            )
        )

    calls = 0

    async def handler(db: AsyncSession) -> dict[str, Any]:
        nonlocal calls
        del db
        calls += 1
        return {"must_not": "run"}

    with pytest.raises(HttpCommandRecoveryRequiredError):
        await _execute(
            factory,
            operation=operation,
            scope=None,
            key=key,
            payload=payload,
            handler=handler,
        )
    assert calls == 0
    async with factory() as db:
        claim = await db.get(HttpCommandClaim, claim_id)
    assert claim is not None and claim.status == "ambiguous"


async def test_different_key_is_rejected_while_same_session_command_is_running(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    operation = f"{_OPERATION_PREFIX}inflight"
    scope = f"session:{uuid.uuid4()}"
    entered = asyncio.Event()
    release = asyncio.Event()

    async def slow_handler(db: AsyncSession) -> dict[str, Any]:
        del db
        entered.set()
        await release.wait()
        return {"ok": True}

    first = asyncio.create_task(
        _execute(
            factory,
            operation=operation,
            scope=scope,
            key="first-key",
            payload={"body": {"value": 1}},
            handler=slow_handler,
        )
    )
    await entered.wait()
    try:
        with pytest.raises(SessionBusyError):
            await _execute(
                factory,
                operation=operation,
                scope=scope,
                key="second-key",
                payload={"body": {"value": 2}},
                handler=slow_handler,
            )
    finally:
        release.set()
        await first


async def test_non_idempotent_commands_share_scope_lock_but_later_random_key_runs_independently(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    operation = f"{_OPERATION_PREFIX}non-idempotent-scope"
    scope = f"session:{uuid.uuid4()}"
    entered = asyncio.Event()
    release = asyncio.Event()
    calls: list[str] = []
    active_handlers = 0
    max_active_handlers = 0
    first_context = _headerless_context()
    overlap_context = _headerless_context()
    later_context = _headerless_context()
    assert not first_context.is_idempotent
    assert not overlap_context.is_idempotent
    assert not later_context.is_idempotent
    assert len(
        {
            first_context.idempotency_key,
            overlap_context.idempotency_key,
            later_context.idempotency_key,
        }
    ) == 3

    async def first_handler(db: AsyncSession) -> dict[str, Any]:
        nonlocal active_handlers, max_active_handlers
        del db
        calls.append("first")
        active_handlers += 1
        max_active_handlers = max(max_active_handlers, active_handlers)
        entered.set()
        try:
            await release.wait()
            return {"command": "first"}
        finally:
            active_handlers -= 1

    async def must_not_overlap(db: AsyncSession) -> dict[str, Any]:
        del db
        calls.append("overlap")
        pytest.fail("a headerless command must not bypass the concurrency scope")

    first = asyncio.create_task(
        _execute(
            factory,
            operation=operation,
            scope=scope,
            key=first_context.idempotency_key,
            payload={"body": {"command": "first"}},
            handler=first_handler,
            is_idempotent=first_context.is_idempotent,
        )
    )
    await entered.wait()
    try:
        with pytest.raises(SessionBusyError):
            await _execute(
                factory,
                operation=operation,
                scope=scope,
                key=overlap_context.idempotency_key,
                payload={"body": {"command": "overlap"}},
                handler=must_not_overlap,
                is_idempotent=overlap_context.is_idempotent,
            )
    finally:
        release.set()
    first_result = await first

    async def later_handler(db: AsyncSession) -> dict[str, Any]:
        nonlocal active_handlers, max_active_handlers
        del db
        calls.append("later")
        active_handlers += 1
        max_active_handlers = max(max_active_handlers, active_handlers)
        active_handlers -= 1
        return {"command": "later"}

    later_result = await _execute(
        factory,
        operation=operation,
        scope=scope,
        key=later_context.idempotency_key,
        payload={"body": {"command": "later"}},
        handler=later_handler,
        is_idempotent=later_context.is_idempotent,
    )

    async with factory() as db:
        claims = (
            await db.scalars(
                select(HttpCommandClaim)
                .where(HttpCommandClaim.operation == operation)
                .order_by(HttpCommandClaim.created_at)
            )
        ).all()
    assert calls == ["first", "later"]
    assert max_active_handlers == 1
    assert not first_result.replayed and not later_result.replayed
    assert first_result.data == {"command": "first"}
    assert later_result.data == {"command": "later"}
    assert len(claims) == 2
    assert {claim.idempotency_mode for claim in claims} == {"non_idempotent"}
    assert {claim.status for claim in claims} == {"completed"}
    assert len({claim.idempotency_key_digest for claim in claims}) == 2


async def test_create_session_api_same_key_different_trace_creates_once(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    patient_ref = f"{_PATIENT_REF_PREFIX}{uuid.uuid4()}"
    public_key = f"create-public-retry-{uuid.uuid4()}"
    public_key_digest = hashlib.sha256(public_key.encode()).hexdigest()
    _PUBLIC_CLAIM_DIGESTS.add(public_key_digest)
    payload = {
        "patient_info": {"patient_ref": patient_ref, "age": 40, "gender": "male"},
        "chief_complaint": "头痛",
        "agent_runtime": "legacy",
    }
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        first, second = await asyncio.gather(
            client.post(
                "/api/v1/consult/sessions",
                json=payload,
                headers={
                    "X-Request-Id": "create-attempt-a",
                    "X-Idempotency-Key": public_key,
                },
            ),
            client.post(
                "/api/v1/consult/sessions",
                json=payload,
                headers={
                    "X-Request-Id": "create-attempt-b",
                    "X-Idempotency-Key": public_key,
                },
            ),
        )

    assert first.status_code == second.status_code == 201
    assert first.json()["data"] == second.json()["data"]
    assert first.json()["trace_id"] != second.json()["trace_id"]
    async with factory() as db:
        session_count = await db.scalar(
            select(func.count())
            .select_from(ConsultSession)
            .where(ConsultSession.patient_ref == patient_ref)
        )
        claim_count = await db.scalar(
            select(func.count())
            .select_from(HttpCommandClaim)
            .where(
                HttpCommandClaim.operation == "session.create.v1",
                HttpCommandClaim.idempotency_key_digest == public_key_digest,
            )
        )
    assert session_count == 1
    assert claim_count == 1
