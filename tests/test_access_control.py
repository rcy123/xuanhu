"""阶段 2 认证测试：会话所有权访问控制（T2.2/T2.3/T2.4 / 验收清单）。

- A 医师带自己的 token 访问 B 医师会话：写接口 → 403 ACCESS_FORBIDDEN；
  读接口 → 404 SESSION_NOT_FOUND（不暴露"存在但不属于你"）。
- 列表查询自动按 owner 过滤。
- 越权写 audit.access_denied（含 attempted_doctor_id、path 模板）。
- audit 模式：校验+记审计但不阻断。
"""

from __future__ import annotations

import contextlib
import os
import uuid
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.auth import hash_password
from app.core.auth import create_access_token
from app.core.redis import reset_redis
from app.main import app
from app.models.audit import AuditEvent
from app.models.consult import ConsultMessage, ConsultSession
from app.models.doctor import Doctor
from app.models.domain import Observation

pytestmark = [pytest.mark.integration, pytest.mark.asyncio(loop_scope="module")]

_PATIENT_REF_PREFIX = "ACCESS-CTL-"


@pytest_asyncio.fixture(scope="module", loop_scope="module", autouse=True)
async def _hardening_on() -> AsyncIterator[None]:
    """auth=on + access=on。"""
    from app.core.config import get_settings

    os.environ["XUANHU_AUTH_ENABLED"] = "on"
    os.environ["XUANHU_ACCESS_ENABLED"] = "on"
    get_settings.cache_clear()
    try:
        yield
    finally:
        os.environ["XUANHU_AUTH_ENABLED"] = "off"
        os.environ["XUANHU_ACCESS_ENABLED"] = "off"
        get_settings.cache_clear()
        with contextlib.suppress(Exception):
            await reset_redis()


@pytest_asyncio.fixture(scope="module", loop_scope="module", autouse=True)
async def _cleanup() -> AsyncIterator[None]:
    from app.db.session import get_session_factory, reset_session_factory

    await reset_session_factory()
    try:
        yield
    finally:
        factory = get_session_factory()
        async with factory() as session:
            result = await session.execute(
                select(ConsultSession.id).where(ConsultSession.patient_ref.like(f"{_PATIENT_REF_PREFIX}%"))
            )
            ids = [row[0] for row in result.all()]
            if ids:
                await session.execute(delete(Observation).where(Observation.session_id.in_(ids)))
                await session.execute(delete(ConsultMessage).where(ConsultMessage.session_id.in_(ids)))
                await session.execute(delete(AuditEvent).where(AuditEvent.session_id.in_(ids)))
                await session.execute(delete(ConsultSession).where(ConsultSession.id.in_(ids)))
            await session.execute(
                delete(Doctor).where(Doctor.name.in_(["医师A", "医师B"]))
            )
            await session.commit()
        with contextlib.suppress(Exception):
            await reset_redis()


@pytest_asyncio.fixture(loop_scope="module")
async def db() -> AsyncSession:
    from app.db.session import get_session_factory

    factory = get_session_factory()
    async with factory() as session:
        yield session


@pytest_asyncio.fixture(loop_scope="module")
async def doctors(db: AsyncSession) -> tuple[Doctor, Doctor]:
    doctor_a = Doctor(name="医师A", password_hash=hash_password("p"), enabled=True)
    doctor_b = Doctor(name="医师B", password_hash=hash_password("p"), enabled=True)
    db.add_all([doctor_a, doctor_b])
    await db.commit()
    await db.refresh(doctor_a)
    await db.refresh(doctor_b)
    yield doctor_a, doctor_b
    await db.execute(delete(Doctor).where(Doctor.id.in_([doctor_a.id, doctor_b.id])))
    await db.commit()


@pytest_asyncio.fixture(loop_scope="module")
async def owned_session(db: AsyncSession, doctors: tuple[Doctor, Doctor]) -> ConsultSession:
    doctor_a, _ = doctors
    session = ConsultSession(
        patient_ref=f"{_PATIENT_REF_PREFIX}{uuid.uuid4().hex[:8]}",
        patient_info={},
        chief_complaint="测试",
        current_stage="inquiry",
        status="active",
        created_by=str(doctor_a.id),
        doctor_id=doctor_a.id,
    )
    db.add(session)
    await db.commit()
    await db.refresh(session)
    yield session
    await db.execute(delete(ConsultSession).where(ConsultSession.id == session.id))
    await db.commit()


def _headers(doctor: Doctor) -> dict[str, str]:
    token, _ = create_access_token(str(doctor.id), name=doctor.name)
    return {"Authorization": f"Bearer {token}"}


@pytest_asyncio.fixture(loop_scope="module")
async def client() -> AsyncClient:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def test_owner_write_allowed(
    client: AsyncClient,
    doctors: tuple[Doctor, Doctor],
    owned_session: ConsultSession,
) -> None:
    """owner 访问自己的写接口不受阻（此处 advance 在 inquiry 阶段 → 业务错误而非 403）。"""
    doctor_a, _ = doctors
    resp = await client.post(
        f"/api/v1/consult/sessions/{owned_session.id}/advance",
        headers=_headers(doctor_a),
        json={"force": False},
    )
    # 不是 403/401 即代表所有权校验通过；inquiry 阶段 advance 返回业务错误
    assert resp.status_code != 403
    assert resp.status_code != 401


async def test_cross_doctor_write_forbidden(
    client: AsyncClient,
    doctors: tuple[Doctor, Doctor],
    owned_session: ConsultSession,
    db: AsyncSession,
) -> None:
    """B 医师写 A 医师会话 → 403 ACCESS_FORBIDDEN + access.denied 审计。"""
    _, doctor_b = doctors
    resp = await client.post(
        f"/api/v1/consult/sessions/{owned_session.id}/advance",
        headers=_headers(doctor_b),
        json={"force": False},
    )
    assert resp.status_code == 403
    assert resp.json()["code"] == "ACCESS_FORBIDDEN"

    # 审计留痕：attempted_doctor_id + path 模板（不含真实 session_id）
    audit = await db.execute(
        select(AuditEvent)
        .where(AuditEvent.event_type == "access.denied")
        .order_by(AuditEvent.created_at.desc())
        .limit(1)
    )
    event = audit.scalar_one_or_none()
    assert event is not None
    assert event.session_id == owned_session.id
    assert event.actor_id == str(doctor_b.id)
    assert event.payload["path"] == "/api/v1/consult/sessions/{session_id}/advance"
    assert event.payload["attempted_doctor_id"] == str(doctor_b.id)
    assert str(owned_session.id) not in str(event.payload["path"])


async def test_cross_doctor_read_not_found(
    client: AsyncClient,
    doctors: tuple[Doctor, Doctor],
    owned_session: ConsultSession,
) -> None:
    """B 医师读 A 医师会话详情 → 404 SESSION_NOT_FOUND（不暴露存在性）。"""
    _, doctor_b = doctors
    resp = await client.get(
        f"/api/v1/consult/sessions/{owned_session.id}",
        headers=_headers(doctor_b),
    )
    assert resp.status_code == 404
    assert resp.json()["code"] == "SESSION_NOT_FOUND"


async def test_error_response_never_leaks_detail(
    client: AsyncClient,
    doctors: tuple[Doctor, Doctor],
    owned_session: ConsultSession,
) -> None:
    """阶段2 T2.7：错误响应 detail 恒为 None，不泄露内部结构化细节。

    覆盖两类：越权读（SessionNotFoundError，detail 原本含 session_id）与
    非法 session_id（ValidationError）。响应体不应出现 session_id / detail 内容。
    """
    _, doctor_b = doctors

    # 越权读 → 404，detail 必须为 None，响应体不出现 session_id
    resp = await client.get(
        f"/api/v1/consult/sessions/{owned_session.id}",
        headers=_headers(doctor_b),
    )
    assert resp.status_code == 404
    body = resp.json()
    assert body["code"] == "SESSION_NOT_FOUND"
    assert body["detail"] is None
    assert str(owned_session.id) not in resp.text

    # 非法 session_id → 400/422，detail 必须为 None（不泄露输入细节）
    resp2 = await client.get(
        "/api/v1/consult/sessions/not-a-valid-uuid",
        headers=_headers(doctor_b),
    )
    assert resp2.status_code in (400, 422)
    body2 = resp2.json()
    assert body2["detail"] is None


async def test_list_filters_by_owner(
    client: AsyncClient,
    doctors: tuple[Doctor, Doctor],
    owned_session: ConsultSession,
) -> None:
    """列表查询按 owner 过滤：B 看不到 A 的会话。"""
    _, doctor_b = doctors
    resp = await client.get("/api/v1/consult/sessions", headers=_headers(doctor_b))
    assert resp.status_code == 200
    data = resp.json()["data"]
    ids = [item["session_id"] for item in data["items"]]
    assert str(owned_session.id) not in ids


async def test_audit_mode_does_not_block(
    client: AsyncClient,
    doctors: tuple[Doctor, Doctor],
    owned_session: ConsultSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """audit 模式：越权校验并记审计，但不阻断（观察期语义）。"""
    from app.core.config import get_settings

    monkeypatch.setenv("XUANHU_ACCESS_ENABLED", "audit")
    get_settings.cache_clear()
    try:
        _, doctor_b = doctors
        resp = await client.post(
            f"/api/v1/consult/sessions/{owned_session.id}/advance",
            headers=_headers(doctor_b),
            json={"force": False},
        )
        assert resp.status_code != 403
        assert resp.status_code != 401
    finally:
        get_settings.cache_clear()
