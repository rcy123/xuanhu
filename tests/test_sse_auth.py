"""阶段 1 认证测试：SSE 事件流鉴权（T1.5 / H1）。

- ``on`` 模式：不带 token 建连 → 立即 401，不产生任何事件。
- ``on`` 模式：带伪造 token → 401。
- ``on`` 模式：带有效 token → 正常建立事件流。
- ``off`` 模式：不带 token → 兼容旧行为可建连。

说明：httpx ASGITransport 会等待 ASGI 应用执行完毕才返回，无法承载
无限 SSE 流；成功建连的用例按项目既有惯例（tests/test_sse_stream.py）
monkeypatch ``iter_sse`` 为有限生成器，认证成功与否看能否越过
``get_current_doctor_from_query`` 拿到 200。
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

from app.core.redis import reset_redis
from app.main import app
from app.models.audit import AuditEvent
from app.models.consult import ConsultMessage, ConsultSession
from app.models.doctor import Doctor
from app.models.domain import Observation
from app.services.events import EventService

pytestmark = [pytest.mark.integration, pytest.mark.asyncio(loop_scope="module")]

_PATIENT_REF_PREFIX = "AUTH-SSE-"


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
            await session.execute(delete(Doctor).where(Doctor.name == "SSE鉴权测试医师"))
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
async def session_and_doctor(db: AsyncSession) -> tuple[ConsultSession, Doctor]:
    from app.api.auth import hash_password

    doctor = Doctor(name="SSE鉴权测试医师", password_hash=hash_password("p"), enabled=True)
    db.add(doctor)
    await db.flush()
    session = ConsultSession(
        patient_ref=f"{_PATIENT_REF_PREFIX}{uuid.uuid4().hex[:8]}",
        patient_info={},
        chief_complaint="测试",
        current_stage="inquiry",
        status="active",
        created_by=str(doctor.id),
    )
    db.add(session)
    await db.commit()
    await db.refresh(doctor)
    await db.refresh(session)
    return session, doctor


def _token(doctor_id: str) -> str:
    from app.core.auth import create_access_token

    token, _ = create_access_token(doctor_id)
    return token


async def _switch_auth(mode: str) -> None:
    from app.core.config import get_settings

    os.environ["XUANHU_AUTH_ENABLED"] = mode
    get_settings.cache_clear()


async def _finite_sse(
    self: object,
    session_id: str,
    *,
    last_event_id: str | None,
    heartbeat_interval_seconds: float,
):
    """有限 SSE 生成器（模拟一帧 resync 后结束），供 ASGI 测试客户端消费。"""
    del self, session_id, last_event_id, heartbeat_interval_seconds
    yield 'event: resync\ndata: {"reason": "test"}\n\n'


@pytest_asyncio.fixture(autouse=True)
async def _reset_auth_after() -> AsyncIterator[None]:
    yield
    await _switch_auth("off")


async def test_sse_no_token_connection_rejected(
    client: AsyncClient, session_and_doctor: tuple[ConsultSession, Doctor]
) -> None:
    """on 模式：SSE 不带 token → 建连即断（401，无任何事件）。"""
    await _switch_auth("on")
    session, _ = session_and_doctor
    async with client.stream("GET", f"/api/v1/consult/sessions/{session.id}/stream") as resp:
        assert resp.status_code == 401
        body = await resp.aread()
    import json

    payload = json.loads(body)
    assert payload["code"] == "UNAUTHENTICATED"


async def test_sse_forged_token_rejected(
    client: AsyncClient, session_and_doctor: tuple[ConsultSession, Doctor]
) -> None:
    """on 模式：SSE 带伪造 token → 401。"""
    await _switch_auth("on")
    session, _ = session_and_doctor
    forged = _token(str(uuid.uuid4())).rsplit(".", 1)[0] + "." + "AAAA"  # 破坏签名
    async with client.stream(
        "GET",
        f"/api/v1/consult/sessions/{session.id}/stream?token={forged}",
    ) as resp:
        assert resp.status_code == 401
        await resp.aread()


async def test_sse_valid_token_streams(
    client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
    session_and_doctor: tuple[ConsultSession, Doctor],
) -> None:
    """on 模式：SSE 带有效 token → 建立事件流。"""
    await _switch_auth("on")
    session, doctor = session_and_doctor
    monkeypatch.setattr(EventService, "iter_sse", _finite_sse)
    token = _token(str(doctor.id))
    response = await client.get(
        f"/api/v1/consult/sessions/{session.id}/stream?token={token}",
    )
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert "resync" in response.text


async def test_sse_off_mode_legacy_connection(
    client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
    session_and_doctor: tuple[ConsultSession, Doctor],
) -> None:
    """off 模式：不带 token 可建连（灰度回退态兼容）。"""
    await _switch_auth("off")
    session, _ = session_and_doctor
    monkeypatch.setattr(EventService, "iter_sse", _finite_sse)
    response = await client.get(f"/api/v1/consult/sessions/{session.id}/stream")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")


@pytest_asyncio.fixture(loop_scope="module")
async def client() -> AsyncClient:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
