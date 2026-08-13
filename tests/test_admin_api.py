"""Integration coverage for fail-closed administrator account management."""

from __future__ import annotations

import contextlib
import os
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient, Response
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.auth import hash_password
from app.core.auth import create_access_token
from app.core.redis import reset_redis
from app.main import app
from app.models.audit import AuditEvent
from app.models.doctor import Doctor

pytestmark = [pytest.mark.integration, pytest.mark.asyncio(loop_scope="module")]

_ADMIN_PASSWORD = "admin-test-password-123"
_DOCTOR_PASSWORD = "doctor-test-password-123"


@dataclass(frozen=True, slots=True)
class _Accounts:
    prefix: str
    admin: Doctor
    doctor: Doctor
    second_admin: Doctor


@pytest_asyncio.fixture(scope="module", loop_scope="module", autouse=True)
async def _admin_auth_environment() -> AsyncIterator[None]:
    """Enable strict auth while keeping account-write tests independent of quotas."""
    from app.core.config import get_settings

    names = (
        "XUANHU_AUTH_ENABLED",
        "JWT_SIGNING_KEY",
        "LOGIN_RATE_LIMIT_PER_MINUTE",
        "XUANHU_RATELIMIT_ENABLED",
    )
    previous = {name: os.environ.get(name) for name in names}
    os.environ["XUANHU_AUTH_ENABLED"] = "on"
    os.environ["JWT_SIGNING_KEY"] = "admin-api-test-signing-key-0123456789"
    os.environ["LOGIN_RATE_LIMIT_PER_MINUTE"] = "600"
    os.environ["XUANHU_RATELIMIT_ENABLED"] = "false"
    get_settings.cache_clear()
    try:
        yield
    finally:
        for name, value in previous.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        get_settings.cache_clear()
        with contextlib.suppress(Exception):
            await reset_redis()


@pytest_asyncio.fixture(loop_scope="module")
async def client() -> AsyncIterator[AsyncClient]:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as test_client:
        yield test_client


@pytest_asyncio.fixture(loop_scope="module")
async def db() -> AsyncIterator[AsyncSession]:
    from app.db.session import get_session_factory, reset_session_factory

    await reset_session_factory()
    factory = get_session_factory()
    async with factory() as session:
        yield session


@pytest_asyncio.fixture(loop_scope="module")
async def accounts(db: AsyncSession) -> AsyncIterator[_Accounts]:
    """Create isolated persisted accounts for every test case."""
    prefix = f"admin-api-{uuid.uuid4().hex}"
    record = _Accounts(
        prefix=prefix,
        admin=Doctor(
            username=f"{prefix}-admin",
            name=f"{prefix}-primary-admin",
            password_hash=hash_password(_ADMIN_PASSWORD),
            role="admin",
            enabled=True,
            auth_version=1,
        ),
        doctor=Doctor(
            username=f"{prefix}-doctor",
            name=f"{prefix}-doctor",
            password_hash=hash_password(_DOCTOR_PASSWORD),
            role="doctor",
            enabled=True,
            auth_version=1,
        ),
        second_admin=Doctor(
            username=f"{prefix}-second-admin",
            name=f"{prefix}-second-admin",
            password_hash=hash_password(_ADMIN_PASSWORD),
            role="admin",
            enabled=True,
            auth_version=1,
        ),
    )
    db.add_all((record.admin, record.doctor, record.second_admin))
    await db.commit()
    for account in (record.admin, record.doctor, record.second_admin):
        await db.refresh(account)
    try:
        yield record
    finally:
        # Admin actions only record the actor ID.  Clear those audit rows first
        # so this fixture remains isolated even if the event schema later gains
        # a foreign key to the operator account.
        await db.execute(delete(AuditEvent).where(AuditEvent.actor_id == str(record.admin.id)))
        await db.execute(delete(Doctor).where(Doctor.name.like(f"{record.prefix}%")))
        await db.commit()


async def _login(client: AsyncClient, account: Doctor, password: str) -> str:
    response = await client.post(
        "/api/v1/auth/login",
        json={"username": account.username, "password": password},
    )
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["user"] == {
        "id": str(account.id),
        "username": account.username,
        "name": account.name,
        "role": account.role,
    }
    return str(data["access_token"])


def _headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _assert_error(response: Response, *, status_code: int, code: str) -> None:
    assert response.status_code == status_code
    body = response.json()
    assert body["code"] == code
    assert body["detail"] is None
    assert body["trace_id"]


async def test_admin_can_list_create_and_soft_disable_doctor(
    client: AsyncClient,
    db: AsyncSession,
    accounts: _Accounts,
) -> None:
    token = await _login(client, accounts.admin, _ADMIN_PASSWORD)
    headers = _headers(token)

    listed = await client.get(f"/api/v1/admin/doctors?q={accounts.doctor.name}", headers=headers)
    assert listed.status_code == 200, listed.text
    listing = listed.json()["data"]
    assert listing["page"] == 1
    assert listing["page_size"] == 20
    assert listing["total"] >= 1
    assert any(item["id"] == str(accounts.doctor.id) for item in listing["items"])

    initial_password = "new-doctor-password-123"
    created_response = await client.post(
        "/api/v1/admin/doctors",
        headers=headers,
        json={"username": f"{accounts.prefix}-created", "name": f"{accounts.prefix}-created", "password": initial_password},
    )
    assert created_response.status_code == 201, created_response.text
    created = created_response.json()["data"]
    assert set(created) == {"id", "username", "name", "role", "enabled", "last_login_at", "created_at"}
    assert created["role"] == "doctor"
    assert created["enabled"] is True
    assert initial_password not in created_response.text
    assert "password_hash" not in created_response.text
    created_id = uuid.UUID(created["id"])

    created_row = await db.scalar(select(Doctor).where(Doctor.id == created_id))
    assert created_row is not None
    assert created_row.role == "doctor"
    assert created_row.enabled is True
    assert created_row.auth_version == 1
    created_audit = await db.scalar(
        select(AuditEvent)
        .where(AuditEvent.event_type == "admin.doctor.created")
        .where(AuditEvent.actor_id == str(accounts.admin.id))
    )
    assert created_audit is not None
    assert created_audit.actor_type == "admin"
    assert created_audit.payload == {"target_doctor_id": str(created_id), "role": "doctor"}
    assert initial_password not in str(created_audit.payload)
    assert "hash" not in str(created_audit.payload).lower()

    # A real login first proves that the old clinical token was valid before
    # the management action changes the authoritative account state.
    created_token = await _login(client, created_row, initial_password)
    before_disable = await client.get("/api/v1/consult/sessions", headers=_headers(created_token))
    assert before_disable.status_code == 200, before_disable.text

    disabled_response = await client.delete(f"/api/v1/admin/doctors/{created_id}", headers=headers)
    assert disabled_response.status_code == 200, disabled_response.text
    disabled = disabled_response.json()["data"]
    assert disabled["id"] == str(created_id)
    assert disabled["role"] == "doctor"
    assert disabled["enabled"] is False
    assert set(disabled) == {"id", "username", "name", "role", "enabled", "last_login_at", "created_at"}

    await db.refresh(created_row)
    assert created_row.enabled is False
    assert created_row.auth_version == 2
    disabled_audit = await db.scalar(
        select(AuditEvent)
        .where(AuditEvent.event_type == "admin.doctor.disabled")
        .where(AuditEvent.actor_id == str(accounts.admin.id))
    )
    assert disabled_audit is not None
    assert disabled_audit.actor_type == "admin"
    assert disabled_audit.payload == {"target_doctor_id": str(created_id), "role": "doctor"}
    assert initial_password not in str(disabled_audit.payload)

    # A disabled account's already-issued bearer token is revoked immediately,
    # while a new login retains the distinct ACCOUNT_DISABLED response.
    revoked = await client.get("/api/v1/consult/sessions", headers=_headers(created_token))
    _assert_error(revoked, status_code=401, code="TOKEN_REVOKED")
    disabled_login = await client.post(
        "/api/v1/auth/login",
        json={"username": f"{accounts.prefix}-created", "password": initial_password},
    )
    _assert_error(disabled_login, status_code=403, code="ACCOUNT_DISABLED")


async def test_doctor_and_claim_mismatch_cannot_access_administration(
    client: AsyncClient,
    db: AsyncSession,
    accounts: _Accounts,
) -> None:
    doctor_token = await _login(client, accounts.doctor, _DOCTOR_PASSWORD)
    headers = _headers(doctor_token)

    listed = await client.get("/api/v1/admin/doctors", headers=headers)
    _assert_error(listed, status_code=403, code="ADMIN_REQUIRED")
    created = await client.post(
        "/api/v1/admin/doctors",
        headers=headers,
        json={"username": f"{accounts.prefix}-must-not-create", "name": f"{accounts.prefix}-must-not-create", "password": "valid-password-123"},
    )
    _assert_error(created, status_code=403, code="ADMIN_REQUIRED")
    disabled = await client.delete(f"/api/v1/admin/doctors/{accounts.doctor.id}", headers=headers)
    _assert_error(disabled, status_code=403, code="ADMIN_REQUIRED")
    await db.refresh(accounts.doctor)
    assert accounts.doctor.enabled is True
    must_not_exist = await db.scalar(select(Doctor.id).where(Doctor.name == f"{accounts.prefix}-must-not-create"))
    assert must_not_exist is None

    # A correctly signed token which only *claims* to be admin is not enough:
    # the role stored on the current account row remains the authority.
    mismatched_token, _ = create_access_token(
        str(accounts.doctor.id),
        name=accounts.doctor.name,
        role="admin",
        auth_version=accounts.doctor.auth_version,
    )
    mismatch = await client.get("/api/v1/admin/doctors", headers=_headers(mismatched_token))
    _assert_error(mismatch, status_code=401, code="TOKEN_REVOKED")


async def test_admin_token_is_rejected_by_clinical_routes(
    client: AsyncClient,
    accounts: _Accounts,
) -> None:
    token = await _login(client, accounts.admin, _ADMIN_PASSWORD)
    response = await client.get("/api/v1/consult/sessions", headers=_headers(token))
    _assert_error(response, status_code=403, code="CLINICAL_ROLE_REQUIRED")


async def test_admin_auth_version_change_revokes_old_token(
    client: AsyncClient,
    db: AsyncSession,
    accounts: _Accounts,
) -> None:
    old_token = await _login(client, accounts.admin, _ADMIN_PASSWORD)
    assert (await client.get("/api/v1/admin/doctors", headers=_headers(old_token))).status_code == 200

    accounts.admin.auth_version += 1
    await db.commit()

    revoked = await client.get("/api/v1/admin/doctors", headers=_headers(old_token))
    _assert_error(revoked, status_code=401, code="TOKEN_REVOKED")
    new_token = await _login(client, accounts.admin, _ADMIN_PASSWORD)
    assert (await client.get("/api/v1/admin/doctors", headers=_headers(new_token))).status_code == 200


async def test_admin_cannot_disable_self_or_an_administrator(
    client: AsyncClient,
    db: AsyncSession,
    accounts: _Accounts,
) -> None:
    token = await _login(client, accounts.admin, _ADMIN_PASSWORD)
    headers = _headers(token)

    self_disable = await client.delete(f"/api/v1/admin/doctors/{accounts.admin.id}", headers=headers)
    _assert_error(self_disable, status_code=409, code="ADMIN_ACTION_FORBIDDEN")
    admin_disable = await client.delete(f"/api/v1/admin/doctors/{accounts.second_admin.id}", headers=headers)
    _assert_error(admin_disable, status_code=409, code="ADMIN_ACTION_FORBIDDEN")
    await db.refresh(accounts.admin)
    await db.refresh(accounts.second_admin)
    assert accounts.admin.enabled is True
    assert accounts.second_admin.enabled is True


async def test_admin_api_stays_fail_closed_when_clinical_auth_is_off(
    client: AsyncClient,
    accounts: _Accounts,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.core.config import get_settings

    with monkeypatch.context() as patch:
        patch.setenv("XUANHU_AUTH_ENABLED", "off")
        get_settings.cache_clear()
        anonymous = await client.get("/api/v1/admin/doctors")
        _assert_error(anonymous, status_code=401, code="UNAUTHENTICATED")

        doctor_token = await _login(client, accounts.doctor, _DOCTOR_PASSWORD)
        doctor = await client.get("/api/v1/admin/doctors", headers=_headers(doctor_token))
        _assert_error(doctor, status_code=403, code="ADMIN_REQUIRED")

        admin_token = await _login(client, accounts.admin, _ADMIN_PASSWORD)
        admin = await client.get("/api/v1/admin/doctors", headers=_headers(admin_token))
        assert admin.status_code == 200, admin.text
    get_settings.cache_clear()


async def test_admin_create_requires_twelve_character_password(
    client: AsyncClient,
    accounts: _Accounts,
) -> None:
    token = await _login(client, accounts.admin, _ADMIN_PASSWORD)
    response = await client.post(
        "/api/v1/admin/doctors",
        headers=_headers(token),
        json={
            "username": f"{accounts.prefix}-short-password",
            "name": f"{accounts.prefix}-short-password",
            "password": "too-short",
        },
    )
    _assert_error(response, status_code=422, code="VALIDATION_ERROR")
