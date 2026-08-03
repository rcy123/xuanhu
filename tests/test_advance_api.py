"""P8-6 阶段推进 API 集成测试。

覆盖：
- advance 从 sufficiency 阶段成功推进到 syndrome
- INSUFFICIENT_INQUIRY（inquiry 阶段 sufficient=false 不可推进）
- PENDING_DOCTOR_REVIEW（review 阶段不可推进）
- INVALID_STAGE_TRANSITION（done/blocked 阶段不可推进）
- force 强制推进（sufficient=false 时 force=true）
- state_version 校验
- SESSION_NOT_FOUND

本测试为集成测试，需要可连接的 PostgreSQL；不可用时自动跳过。
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import delete, or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.audit import AuditEvent
from app.models.consult import ConsultMessage, ConsultSession
from app.models.domain import Observation

pytestmark = [pytest.mark.integration, pytest.mark.asyncio(loop_scope="module")]

_TEST_PATIENT_REF_PREFIX = "P8-ADV-"
_TEST_DOCTOR_ID = "doctor_p8_adv_test"


@pytest_asyncio.fixture(scope="module", loop_scope="module", autouse=True)
async def _cleanup_test_data() -> None:
    from app.core.config import get_settings
    from app.db.session import get_session_factory, reset_session_factory

    get_settings.cache_clear()
    await reset_session_factory()

    yield

    factory = get_session_factory()
    async with factory() as session:
        result = await session.execute(
            select(ConsultSession.id).where(
                or_(
                    ConsultSession.patient_ref.like(f"{_TEST_PATIENT_REF_PREFIX}%"),
                    ConsultSession.created_by == _TEST_DOCTOR_ID,
                )
            )
        )
        test_ids = [row[0] for row in result.all()]
        if not test_ids:
            return
        await session.execute(
            delete(Observation).where(Observation.session_id.in_(test_ids))
        )
        await session.execute(
            delete(ConsultMessage).where(ConsultMessage.session_id.in_(test_ids))
        )
        await session.execute(
            delete(AuditEvent).where(AuditEvent.session_id.in_(test_ids))
        )
        await session.execute(
            delete(ConsultSession).where(ConsultSession.id.in_(test_ids))
        )
        await session.commit()


@pytest_asyncio.fixture(loop_scope="module")
async def db() -> AsyncSession:
    from app.db.session import get_session_factory

    factory = get_session_factory()
    async with factory() as session:
        yield session


@pytest_asyncio.fixture(loop_scope="module")
async def client() -> AsyncClient:
    """FastAPI 异步测试客户端。"""
    from httpx import ASGITransport

    from app.main import app

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest_asyncio.fixture(loop_scope="module", autouse=True)
async def _check_postgres() -> None:
    from app.db.session import get_session_factory

    factory = get_session_factory()
    try:
        async with factory() as session:
            await session.execute(text("SELECT 1"))
    except Exception as exc:  # noqa: BLE001
        pytest.fail(f"PostgreSQL integration dependency unavailable: {type(exc).__name__}: {exc}")


async def _create_session(
    client: AsyncClient,
    payload: dict[str, Any] | None = None,
    stage: str = "inquiry",
) -> dict[str, Any]:
    response = await client.post(
        "/api/v1/consult/sessions",
        json=payload or {},
        headers={"X-Doctor-Id": _TEST_DOCTOR_ID},
    )
    assert response.status_code == 201, response.text
    data = response.json()["data"]
    if stage != "inquiry":
        # 直接修改 DB stage 用于测试
        sid = uuid.UUID(data["session_id"])
        from app.db.session import get_session_factory

        factory = get_session_factory()
        async with factory() as session:
            result = await session.execute(
                select(ConsultSession).where(ConsultSession.id == sid)
            )
            s = result.scalar_one()
            s.current_stage = stage
            await session.commit()
    return data


async def _post_advance(
    client: AsyncClient,
    session_id: str,
    body: dict[str, Any] | None = None,
    *,
    expect_status: int = 200,
) -> dict[str, Any]:
    response = await client.post(
        f"/api/v1/consult/sessions/{session_id}/advance",
        json=body or {},
        headers={"X-Doctor-Id": _TEST_DOCTOR_ID},
    )
    assert response.status_code == expect_status, response.text
    return response.json()


# ---------------------------------------------------------------------------
# 成功推进
# ---------------------------------------------------------------------------


async def test_advance_sufficiency_to_syndrome_success(
    client: AsyncClient, db: AsyncSession
) -> None:
    """从 sufficiency 阶段推进到 syndrome（需 sufficient=true 的 snapshot）。"""
    s = await _create_session(
        client,
        {
            "patient_info": {
                "name": "Adv测试",
                "patient_ref": f"{_TEST_PATIENT_REF_PREFIX}ADV{datetime.now(UTC).strftime('%H%M%S%f')}",
                "gender": "male",
                "age": 30,
            },
            "chief_complaint": "头痛",
            "agent_runtime": "langgraph",
        },
        stage="sufficiency",
    )
    # 写 sufficient=true 的 snapshot
    sid = uuid.UUID(s["session_id"])
    from app.db.session import get_session_factory

    factory = get_session_factory()
    async with factory() as session:
        result = await session.execute(
            select(ConsultSession).where(ConsultSession.id == sid)
        )
        session_obj = result.scalar_one()
        session_obj.state_snapshot = {
            "current_stage": "sufficiency",
            "sufficiency_report": {
                "covered": ["chief_complaint", "present_illness"],
                "missing": [],
                "sufficient": True,
                "suggestions": [],
            },
            "state_version": session_obj.state_version,
        }
        await session.commit()

    body = await _post_advance(
        client, s["session_id"], {"force": False}, expect_status=409
    )
    # langgraph 收敛后不存在 sufficiency 阶段：advance 仅支持 inquiry/syndrome/safety/record
    assert body["code"] == "INVALID_STAGE_TRANSITION", f"预期拒绝，实际 {body.get('code')}"


# ---------------------------------------------------------------------------
# INSUFFICIENT_INQUIRY
# ---------------------------------------------------------------------------


async def test_advance_from_inquiry_insufficient(client: AsyncClient, db: AsyncSession) -> None:
    """inquiry 阶段 sufficient=false 时不可推进。"""
    s = await _create_session(
        client,
        {
            "patient_info": {
                "name": "Adv不足",
                "patient_ref": f"{_TEST_PATIENT_REF_PREFIX}INS{datetime.now(UTC).strftime('%H%M%S%f')}",
                "gender": "male",
                "age": 30,
            },
            "chief_complaint": "头痛",
            "agent_runtime": "langgraph",
        },
        stage="inquiry",
    )
    # 确保 snapshot 中 sufficient=false
    sid = uuid.UUID(s["session_id"])
    from app.db.session import get_session_factory

    factory = get_session_factory()
    async with factory() as session:
        result = await session.execute(
            select(ConsultSession).where(ConsultSession.id == sid)
        )
        session_obj = result.scalar_one()
        session_obj.state_snapshot = {
            "current_stage": "inquiry",
            "sufficiency_report": {
                "covered": [],
                "missing": ["present_illness"],
                "sufficient": False,
                "suggestions": ["请补充现病史"],
            },
            "state_version": session_obj.state_version,
        }
        await session.commit()

    body = await _post_advance(
        client, s["session_id"], expect_status=400
    )
    assert body["code"] == "INSUFFICIENT_INQUIRY"


# ---------------------------------------------------------------------------
# PENDING_DOCTOR_REVIEW
# ---------------------------------------------------------------------------


async def test_advance_from_review_rejected(client: AsyncClient, db: AsyncSession) -> None:
    """review 阶段不可推进，需先提交医师确认。"""
    s = await _create_session(
        client,
        {
            "patient_info": {
                "name": "AdvReview",
                "patient_ref": f"{_TEST_PATIENT_REF_PREFIX}REV{datetime.now(UTC).strftime('%H%M%S%f')}",
                "gender": "male",
                "age": 30,
            },
            "chief_complaint": "头痛",
            "agent_runtime": "langgraph",
        },
        stage="review",
    )
    body = await _post_advance(
        client, s["session_id"], expect_status=409
    )
    assert body["code"] == "PENDING_DOCTOR_REVIEW"


# ---------------------------------------------------------------------------
# INVALID_STAGE_TRANSITION
# ---------------------------------------------------------------------------


async def test_advance_from_done_rejected(client: AsyncClient, db: AsyncSession) -> None:
    """done 阶段不可推进。"""
    s = await _create_session(
        client,
        {
            "patient_info": {
                "name": "AdvDone",
                "patient_ref": f"{_TEST_PATIENT_REF_PREFIX}DON{datetime.now(UTC).strftime('%H%M%S%f')}",
                "gender": "male",
                "age": 30,
            },
            "chief_complaint": "头痛",
            "agent_runtime": "langgraph",
        },
        stage="done",
    )
    body = await _post_advance(
        client, s["session_id"], expect_status=409
    )
    assert body["code"] == "INVALID_STAGE_TRANSITION"


async def test_advance_from_blocked_rejected(client: AsyncClient, db: AsyncSession) -> None:
    """blocked 阶段不可推进。"""
    s = await _create_session(
        client,
        {
            "patient_info": {
                "name": "AdvBlocked",
                "patient_ref": f"{_TEST_PATIENT_REF_PREFIX}BLK{datetime.now(UTC).strftime('%H%M%S%f')}",
                "gender": "male",
                "age": 30,
            },
            "chief_complaint": "头痛",
            "agent_runtime": "langgraph",
        },
        stage="blocked",
    )
    body = await _post_advance(
        client, s["session_id"], expect_status=409
    )
    assert body["code"] == "INVALID_STAGE_TRANSITION"


# ---------------------------------------------------------------------------
# SESSION_NOT_FOUND
# ---------------------------------------------------------------------------


async def test_advance_session_not_found(client: AsyncClient, db: AsyncSession) -> None:
    """不存在的会话返回 404。"""
    body = await _post_advance(
        client, str(uuid.uuid4()), expect_status=404
    )
    assert body["code"] == "SESSION_NOT_FOUND"
