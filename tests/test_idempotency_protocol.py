"""Public idempotency protocol boundary tests."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from importlib import import_module
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import Request
from httpx import ASGITransport, AsyncClient

from app.api.advance import _advance_command_key
from app.api.request_context import validate_idempotency_key, write_request_context
from app.core.exceptions import IdempotencyConflictError, ValidationError
from app.main import app
from app.models.http_command import HttpCommandClaim
from app.schemas.message import MessageCreateResponse
from app.services.http_idempotency import HttpCommandResult
from app.services.langgraph_intake import _command_key


def _request(*headers: tuple[bytes, bytes]) -> Request:
    return Request(
        {
            "type": "http",
            "http_version": "1.1",
            "method": "POST",
            "scheme": "http",
            "path": "/",
            "raw_path": b"/",
            "query_string": b"",
            "headers": list(headers),
            "client": ("127.0.0.1", 1),
            "server": ("test", 80),
        }
    )


def _install_passthrough_executor(monkeypatch: pytest.MonkeyPatch) -> None:
    async def preflight(*args: object, **kwargs: object) -> None:
        del args, kwargs

    async def execute(self: object, **kwargs: Any) -> HttpCommandResult:
        del self
        data = await kwargs["handler"]()
        return HttpCommandResult(
            data=data,
            status_code=kwargs["success_status"],
            message=kwargs["success_message"],
            replayed=False,
        )

    monkeypatch.setattr("app.services.http_idempotency.HttpCommandExecutor.execute", execute)
    monkeypatch.setattr(
        "app.api.messages.MessageService.ensure_submission_runtime_available",
        preflight,
    )


def test_http_executor_rejects_invalid_renew_attempt_budget() -> None:
    from app.services.http_idempotency import HttpCommandExecutor

    with pytest.raises(ValueError):
        HttpCommandExecutor(None, session_factory=object(), renew_attempt_seconds=0)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        HttpCommandExecutor(None, session_factory=object(), heartbeat_seconds=2, lease_seconds=1)  # type: ignore[arg-type]


def test_http_executor_decouples_renew_attempt_budget_from_heartbeat() -> None:
    from app.services.http_idempotency import HttpCommandExecutor

    explicit = HttpCommandExecutor(
        None,  # type: ignore[arg-type]
        session_factory=object(),
        renew_attempt_seconds=1.5,
    )
    assert explicit._renew_attempt_seconds == 1.5

    # Default: the per-attempt budget follows the heartbeat (unchanged behaviour).
    defaulted = HttpCommandExecutor(None, session_factory=object())  # type: ignore[arg-type]
    assert defaulted._renew_attempt_seconds is None


@pytest.mark.parametrize(
    "value",
    [
        "",
        "a" * 129,
        " leading",
        "trailing ",
        "contains space",
        "comma,value",
        "slash/value",
        "line\rbreak",
        "line\nbreak",
        "nul\x00byte",
        "中文",
    ],
)
def test_idempotency_key_rejects_invalid_length_characters_and_crlf(value: str) -> None:
    with pytest.raises(ValidationError):
        validate_idempotency_key(value)


def test_idempotency_key_accepts_bounded_ascii_token() -> None:
    value = "Request.2026_07-13:retry-1"
    assert validate_idempotency_key(value) == value
    assert validate_idempotency_key("a" * 128) == "a" * 128


def test_write_context_rejects_duplicate_idempotency_headers() -> None:
    request = _request(
        (b"x-idempotency-key", b"first"),
        (b"x-idempotency-key", b"second"),
    )
    with pytest.raises(ValidationError):
        write_request_context(request)


def test_missing_idempotency_key_creates_independent_non_idempotent_commands() -> None:
    headers = ((b"x-request-id", b"same-trace"),)
    first = write_request_context(_request(*headers))
    second = write_request_context(_request(*headers))

    assert first.trace_id == second.trace_id == "same-trace"
    assert not first.is_idempotent and not second.is_idempotent
    assert first.idempotency_key != second.idempotency_key
    assert _command_key(first.idempotency_key) != _command_key(second.idempotency_key)
    assert _advance_command_key(first.idempotency_key) != _advance_command_key(second.idempotency_key)


def test_public_key_derives_stable_command_independent_of_trace() -> None:
    first = write_request_context(
        _request(
            (b"x-request-id", b"trace-a"),
            (b"x-idempotency-key", b"public-retry-key"),
        )
    )
    second = write_request_context(
        _request(
            (b"x-request-id", b"trace-b"),
            (b"x-idempotency-key", b"public-retry-key"),
        )
    )

    assert first.trace_id != second.trace_id
    assert first.is_idempotent and second.is_idempotent
    assert first.idempotency_key == second.idempotency_key
    assert _command_key(first.idempotency_key) == _command_key(second.idempotency_key)
    assert _advance_command_key(first.idempotency_key) == _advance_command_key(second.idempotency_key)


def test_public_write_openapi_exposes_idempotency_header() -> None:
    schema = app.openapi()
    for path, method in (
        ("/api/v1/consult/sessions", "post"),
        ("/api/v1/consult/sessions/{session_id}/terminate", "post"),
        ("/api/v1/consult/sessions/{session_id}/messages", "post"),
        ("/api/v1/consult/sessions/{session_id}/advance", "post"),
        ("/api/v1/consult/sessions/{session_id}/review", "post"),
        ("/api/v1/consult/sessions/{session_id}/recover", "post"),
        ("/api/v1/consult/sessions/{session_id}/record", "put"),
        (
            "/api/v1/consult/sessions/{session_id}/safety-assertions/"
            "{assertion_id}/confirm",
            "post",
        ),
        (
            "/api/v1/consult/sessions/{session_id}/safety-assertions/"
            "{assertion_id}/reject",
            "post",
        ),
        (
            "/api/v1/consult/sessions/{session_id}/safety-assertions/"
            "{assertion_id}/retract",
            "post",
        ),
    ):
        parameters = schema["paths"][path][method]["parameters"]
        assert any(
            item["in"] == "header" and item["name"] == "X-Idempotency-Key"
            for item in parameters
        )


def test_http_command_claim_schema_has_durable_uniqueness_and_inflight_guard() -> None:
    table = HttpCommandClaim.__table__
    constraint_names = {constraint.name for constraint in table.constraints}
    index_names = {index.name for index in table.indexes}

    assert "uq_http_command_claims_logical_command" in constraint_names
    assert any(
        name is not None and name.endswith("chk_http_command_claims_completed_payload")
        for name in constraint_names
    )
    assert "uq_http_command_claims_inflight_scope" in index_names
    assert "idx_http_command_claims_status_lease" in index_names


def test_http_command_claim_migration_is_on_current_chain() -> None:
    migration = import_module(
        "app.db.migrations.versions.20260713_0008_http_command_claims"
    )
    assert migration.revision == "20260713_0008"
    assert migration.down_revision == "20260712_0007"
    assert callable(migration.upgrade)
    assert callable(migration.downgrade)


@pytest.mark.asyncio
async def test_messages_route_consumes_public_key_across_different_traces(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: list[dict[str, Any]] = []
    _install_passthrough_executor(monkeypatch)

    async def fake_submit_message(
        self: object,
        session_id: str,
        body: object,
        **kwargs: Any,
    ) -> MessageCreateResponse:
        del self, body
        captured.append(kwargs)
        return MessageCreateResponse(
            message_id=str(uuid.uuid4()),
            session_id=session_id,
            role="doctor",
            stage="inquiry",
            content="test",
            current_stage="inquiry",
            state_version=1,
            created_at=datetime.now(UTC),
        )

    monkeypatch.setattr("app.api.messages.MessageService.submit_message", fake_submit_message)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        for trace_id in ("trace-a", "trace-b"):
            response = await client.post(
                f"/api/v1/consult/sessions/{uuid.uuid4()}/messages",
                json={"role": "doctor", "content": "test"},
                headers={
                    "X-Request-Id": trace_id,
                    "X-Idempotency-Key": "same-public-message-key",
                },
            )
            assert response.status_code == 200, response.text

    assert [item["trace_id"] for item in captured] == ["trace-a", "trace-b"]
    assert [item["idempotency_key"] for item in captured] == [
        "same-public-message-key",
        "same-public-message-key",
    ]


@pytest.mark.asyncio
async def test_advance_route_consumes_public_key_across_different_traces(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: list[dict[str, Any]] = []
    _install_passthrough_executor(monkeypatch)

    async def fake_load(db: object, session_id: str) -> SimpleNamespace:
        del db
        return SimpleNamespace(
            id=uuid.UUID(session_id),
            agent_runtime="langgraph",
            recovery_status="normal",
        )

    async def no_durable_replay(**_kwargs: object) -> None:
        return None

    async def fake_advance(db: object, session: object, **kwargs: Any) -> dict[str, Any]:
        del db, session
        captured.append(kwargs)
        return {"session_id": kwargs["session_id"], "current_stage": "review", "state_version": 2}

    monkeypatch.setattr("app.api.advance._load_session_for_advance", fake_load)
    monkeypatch.setattr("app.api.advance._repair_durable_advance_claim", no_durable_replay)
    monkeypatch.setattr("app.api.advance._run_langgraph_advance", fake_advance)
    session_id = str(uuid.uuid4())
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        for trace_id in ("advance-trace-a", "advance-trace-b"):
            response = await client.post(
                f"/api/v1/consult/sessions/{session_id}/advance",
                json={"force": False},
                headers={
                    "X-Request-Id": trace_id,
                    "X-Idempotency-Key": "same-public-advance-key",
                },
            )
            assert response.status_code == 200, response.text

    assert [item["trace_id"] for item in captured] == ["advance-trace-a", "advance-trace-b"]
    assert [item["idempotency_key"] for item in captured] == [
        "same-public-advance-key",
        "same-public-advance-key",
    ]


@pytest.mark.asyncio
async def test_write_routes_reject_malformed_public_key_before_service_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def should_not_run(*args: object, **kwargs: object) -> None:
        del args, kwargs
        pytest.fail("service must not run for an invalid idempotency key")

    monkeypatch.setattr("app.api.messages.MessageService.submit_message", should_not_run)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            f"/api/v1/consult/sessions/{uuid.uuid4()}/messages",
            json={"role": "doctor", "content": "test"},
            headers={"X-Idempotency-Key": "invalid key"},
        )

    assert response.status_code == 422
    assert response.json()["code"] == "VALIDATION_ERROR"


@pytest.mark.asyncio
async def test_payload_digest_conflict_has_stable_409_envelope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_passthrough_executor(monkeypatch)

    async def conflict(*args: object, **kwargs: object) -> None:
        del args, kwargs
        raise IdempotencyConflictError(detail="payload_digest_mismatch")

    monkeypatch.setattr("app.api.messages.MessageService.submit_message", conflict)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            f"/api/v1/consult/sessions/{uuid.uuid4()}/messages",
            json={"role": "doctor", "content": "different payload"},
            headers={
                "X-Request-Id": "retry-trace",
                "X-Idempotency-Key": "already-claimed-key",
            },
        )

    assert response.status_code == 409
    assert response.json() == {
        "code": "IDEMPOTENCY_KEY_REUSED",
        "message": "相同幂等键不能用于不同请求",
        "detail": None,  # 阶段2 T2.7：detail 不回传客户端
        "retryable": False,
        "stage": None,
        "trace_id": "retry-trace",
    }
