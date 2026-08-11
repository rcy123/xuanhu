"""R6-A public status API unit tests.

The endpoint instantiates the real Postgres repository, so these tests
monkeypatch ``app.api.commands.PostgresAsyncCommandRepository`` with a fake to
drive the routing/envelope logic without a database. Privacy, cross-session
indistinguishability and all-lifecycle states are additionally exercised
against real PostgreSQL by the integration marker.
"""

from __future__ import annotations

import uuid
from typing import Any

from fastapi.testclient import TestClient

from app.agent_runtime.async_command import AsyncCommandStatus
from app.main import app

client = TestClient(app)  # lifespan not run; DB-free by design

_CMD_ID = uuid.uuid4()
_SESSION_ID = uuid.uuid4()


class _FakeRepository:
    def __init__(self, session_factory: Any = None) -> None:
        del session_factory
        self._status: AsyncCommandStatus | None = None
        self._session_exists: bool = True

    def set_status(self, status: AsyncCommandStatus | None) -> None:
        self._status = status

    def set_session_exists(self, exists: bool) -> None:
        self._session_exists = exists

    async def get_status(self, session_id: uuid.UUID, command_id: uuid.UUID) -> AsyncCommandStatus | None:
        del session_id, command_id
        return self._status

    async def session_exists(self, session_id: uuid.UUID) -> bool:
        del session_id
        return self._session_exists


def _patch_repo(monkeypatch: Any, fake: _FakeRepository) -> None:
    monkeypatch.setattr("app.api.commands.PostgresAsyncCommandRepository", lambda factory: fake)


def _queued_status() -> AsyncCommandStatus:
    return AsyncCommandStatus(
        command_id=_CMD_ID,
        operation="session.advance",
        status="queued",
        attempt_count=0,
        result_http_status=None,
        error_code=None,
        created_at=None,
        started_at=None,
        completed_at=None,
        updated_at=None,
    )


def _url(sid: uuid.UUID = _SESSION_ID, cid: uuid.UUID = _CMD_ID) -> str:
    return f"/api/v1/consult/sessions/{sid}/commands/{cid}"


def test_queued_status_returns_200_with_envelope(monkeypatch: Any) -> None:
    fake = _FakeRepository()
    fake.set_status(_queued_status())
    _patch_repo(monkeypatch, fake)

    response = client.get(_url())
    assert response.status_code == 200  # status query, not a 202 acceptance
    body = response.json()["data"]
    assert body["command_id"] == str(_CMD_ID)
    assert body["operation"] == "session.advance"
    assert body["status"] == "queued"
    assert body["attempt_count"] == 0
    assert body["result"] is None
    assert body["error"] is None
    assert body["links"]["self"].endswith(f"/commands/{_CMD_ID}")


def test_malformed_session_uses_session_not_found_envelope(monkeypatch: Any) -> None:
    fake = _FakeRepository()
    _patch_repo(monkeypatch, fake)
    response = client.get("/api/v1/consult/sessions/not-a-uuid/commands/not-a-uuid")
    assert response.status_code == 404
    assert response.json()["code"] == "SESSION_NOT_FOUND"


def test_malformed_command_id_is_command_not_found(monkeypatch: Any) -> None:
    fake = _FakeRepository()
    _patch_repo(monkeypatch, fake)
    response = client.get(_url(cid="not-a-uuid"))
    assert response.status_code == 404
    assert response.json()["code"] == "COMMAND_NOT_FOUND"


def test_missing_session_uses_session_not_found_envelope(monkeypatch: Any) -> None:
    fake = _FakeRepository()
    fake.set_session_exists(False)
    _patch_repo(monkeypatch, fake)
    response = client.get(_url())
    assert response.status_code == 404
    assert response.json()["code"] == "SESSION_NOT_FOUND"


def test_missing_command_is_indistinguishable_from_cross_session(monkeypatch: Any) -> None:
    fake = _FakeRepository()  # session exists; get_status returns None -> not found
    _patch_repo(monkeypatch, fake)

    own = client.get(_url())
    other = client.get(_url(sid=uuid.uuid4()))
    assert own.status_code == 404
    assert other.status_code == 404
    assert own.json()["code"] == other.json()["code"] == "COMMAND_NOT_FOUND"


def test_404_envelope_matches_standard_error_shape(monkeypatch: Any) -> None:
    fake = _FakeRepository()
    _patch_repo(monkeypatch, fake)
    response = client.get(_url(cid=uuid.uuid4()))
    payload = response.json()
    for key in ("code", "message", "detail", "retryable", "stage", "trace_id"):
        assert key in payload
    assert payload["retryable"] is False
