"""P7-3 病历编辑与导出 API 测试。

覆盖：
- GET record（latest / 指定 version / 无病历 / 无会话）
- PUT record（编辑成功 / 多版本 / 非法阶段 / state_version 冲突 / 锁冲突 / 无病历）
- GET export（txt / json / md / 不支持的格式 / 无病历）
- 审计事件（record.edited / record.exported）

本测试为集成测试，需要可连接的 PostgreSQL + Redis；不可用时自动跳过。
"""

from __future__ import annotations

import contextlib
import uuid
from typing import Any

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.main import app
from app.models.audit import AuditEvent
from app.models.consult import ConsultSession
from app.models.review import DoctorReview, MedicalRecord

pytestmark = [pytest.mark.integration, pytest.mark.asyncio(loop_scope="module")]


# ---------------------------------------------------------------------------
# 模块级测试数据
# ---------------------------------------------------------------------------

_TEST_PATIENT_REF_PREFIX = "P7-3-RECORD-"
_TEST_DOCTOR_ID = "doctor_p7_3_record"


@pytest_asyncio.fixture(scope="module", loop_scope="module", autouse=True)
async def _cleanup_test_sessions() -> None:
    """模块结束时清理本模块创建的会话及关联数据。"""
    yield

    from app.db.session import get_session_factory

    factory = get_session_factory()
    async with factory() as session:
        try:
            await session.execute(select(ConsultSession.id).limit(1))
        except Exception:  # noqa: BLE001
            return

        session_ids_subq = select(ConsultSession.id).where(
            or_(
                ConsultSession.patient_ref.like(f"{_TEST_PATIENT_REF_PREFIX}%"),
                ConsultSession.created_by == _TEST_DOCTOR_ID,
            )
        )
        await session.execute(
            delete(MedicalRecord).where(
                MedicalRecord.session_id.in_(session_ids_subq)
            )
        )
        await session.execute(
            delete(DoctorReview).where(
                DoctorReview.session_id.in_(session_ids_subq)
            )
        )
        await session.execute(
            delete(AuditEvent).where(
                AuditEvent.session_id.in_(session_ids_subq)
            )
        )
        await session.execute(
            delete(ConsultSession).where(
                or_(
                    ConsultSession.patient_ref.like(f"{_TEST_PATIENT_REF_PREFIX}%"),
                    ConsultSession.created_by == _TEST_DOCTOR_ID,
                )
            )
        )
        await session.commit()


@pytest_asyncio.fixture(loop_scope="module")
async def db() -> AsyncSession:
    """提供独立数据库会话。"""
    from app.db.session import get_session_factory

    factory = get_session_factory()
    async with factory() as session:
        yield session


@pytest_asyncio.fixture(loop_scope="module")
async def client() -> AsyncClient:
    """FastAPI 异步测试客户端。"""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest_asyncio.fixture(scope="module", loop_scope="module", autouse=True)
async def _check_postgres() -> None:
    """检查 PostgreSQL 可用性。"""
    from app.core.config import get_settings
    from app.db.session import get_session_factory, reset_session_factory

    get_settings()
    await reset_session_factory()
    factory = get_session_factory()

    try:
        async with factory() as session:
            await session.execute(text("SELECT 1"))
    except (OSError, ConnectionError) as exc:
        pytest.fail(
            f"PostgreSQL integration dependency unavailable: {type(exc).__name__}: {exc}"
        )
    except Exception as exc:  # noqa: BLE001
        pytest.fail(
            f"PostgreSQL 检查出现非连接类异常: {type(exc).__name__}: {exc}",
            pytrace=True,
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _record_headers(
    *,
    doctor_id: str = _TEST_DOCTOR_ID,
    state_version: int | None = None,
) -> dict[str, str]:
    headers: dict[str, str] = {"X-Doctor-Id": doctor_id}
    if state_version is not None:
        headers["X-State-Version"] = str(state_version)
    return headers


async def _create_session(
    db: AsyncSession,
    *,
    stage: str = "record",
    status: str = "active",
    patient_ref: str | None = None,
) -> ConsultSession:
    """创建测试会话。"""
    session_id = uuid.uuid4()
    ref = patient_ref or f"{_TEST_PATIENT_REF_PREFIX}{session_id.hex[:8]}"
    session = ConsultSession(
        id=session_id,
        patient_ref=ref,
        patient_info={"gender": "male", "age": 35},
        current_stage=stage,
        status=status,
        pending_review=False,
        state_version=1,
        rollback_counts={},
        state_snapshot={
            "current_stage": stage,
            "pending_review": False,
            "state_version": 1,
        },
        created_by=_TEST_DOCTOR_ID,
    )
    db.add(session)
    await db.commit()
    await db.refresh(session)
    return session


async def _create_doctor_review(
    db: AsyncSession,
    session: ConsultSession,
    *,
    action: str = "confirm",
) -> DoctorReview:
    """创建一条 doctor_review 记录。"""
    review = DoctorReview(
        id=uuid.uuid4(),
        session_id=session.id,
        agent_run_id=None,
        safety_rule_run_id=None,
        action=action,
        original_formula=None,
        formula_override=None,
        feedback=None,
        reviewed_by=_TEST_DOCTOR_ID,
    )
    db.add(review)
    await db.commit()
    await db.refresh(review)
    return review


async def _create_medical_record(
    db: AsyncSession,
    session: ConsultSession,
    *,
    version: int = 1,
    record_text: str | None = None,
    record_json: dict[str, Any] | None = None,
    doctor_review_id: uuid.UUID | None = None,
    edited_by_doctor: bool = False,
) -> MedicalRecord:
    """创建一条 medical_record（用于测试编辑/导出）。"""
    record = MedicalRecord(
        id=uuid.uuid4(),
        session_id=session.id,
        version=version,
        record_text=record_text or "【主诉】头痛3天\n【辨证】风寒束表证\n【处方】麻黄汤...",
        record_json=record_json or {
            "chief_complaint": "头痛3天",
            "syndrome": "风寒束表证",
            "formula": {
                "name": "麻黄汤",
                "composition": [{"herb": "麻黄", "dose": 9, "unit": "g"}],
            },
        },
        diff_from_previous=None,
        doctor_review_id=doctor_review_id,
        disclaimer="本记录由悬壶AI辅助生成，经医师审核确认。仅供参考。",
        edited_by_doctor=edited_by_doctor,
    )
    db.add(record)
    await db.commit()
    await db.refresh(record)
    return record


async def _count_audit_events(
    db: AsyncSession, session_id: str, event_type: str
) -> int:
    sid = uuid.UUID(session_id)
    result = await db.execute(
        select(AuditEvent).where(
            AuditEvent.session_id == sid,
            AuditEvent.event_type == event_type,
        )
    )
    return len(result.scalars().all())


# ---------------------------------------------------------------------------
# GET /record
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio(loop_scope="module")
async def test_get_record_latest(
    client: AsyncClient, db: AsyncSession
) -> None:
    """获取最新版本病历成功。"""
    session = await _create_session(db, stage="record")
    review = await _create_doctor_review(db, session)
    record = await _create_medical_record(
        db, session, version=1, doctor_review_id=review.id
    )
    try:
        resp = await client.get(
            f"/api/v1/consult/sessions/{session.id}/record",
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["code"] == "SUCCESS"
        data = body["data"]
        assert data["id"] == str(record.id)
        assert data["session_id"] == str(session.id)
        assert data["version"] == 1
        assert data["record_text"] == record.record_text
        assert data["record_json"] == record.record_json
        assert data["edited_by_doctor"] is False
        assert data["doctor_review_id"] == str(review.id)
        assert data["disclaimer"] == record.disclaimer
    finally:
        pass


@pytest.mark.integration
@pytest.mark.asyncio(loop_scope="module")
async def test_get_record_specific_version(
    client: AsyncClient, db: AsyncSession
) -> None:
    """获取指定版本病历成功。"""
    session = await _create_session(db, stage="done")
    review = await _create_doctor_review(db, session)
    await _create_medical_record(
        db, session, version=1, doctor_review_id=review.id,
        record_text="v1 text",
    )
    await _create_medical_record(
        db, session, version=2, doctor_review_id=review.id,
        record_text="v2 text",
        edited_by_doctor=True,
    )
    try:
        # 获取 latest
        resp1 = await client.get(
            f"/api/v1/consult/sessions/{session.id}/record?version=latest",
        )
        assert resp1.status_code == 200, resp1.text
        assert resp1.json()["data"]["version"] == 2
        assert resp1.json()["data"]["record_text"] == "v2 text"

        # 获取 version=1
        resp2 = await client.get(
            f"/api/v1/consult/sessions/{session.id}/record?version=1",
        )
        assert resp2.status_code == 200, resp2.text
        assert resp2.json()["data"]["version"] == 1
        assert resp2.json()["data"]["record_text"] == "v1 text"
    finally:
        pass


@pytest.mark.integration
@pytest.mark.asyncio(loop_scope="module")
async def test_get_record_session_not_found(client: AsyncClient) -> None:
    """会话不存在 → SESSION_NOT_FOUND。"""
    fake_id = uuid.uuid4()
    resp = await client.get(
        f"/api/v1/consult/sessions/{fake_id}/record",
    )
    assert resp.status_code == 404, resp.text
    assert resp.json()["code"] == "SESSION_NOT_FOUND"


@pytest.mark.integration
@pytest.mark.asyncio(loop_scope="module")
async def test_get_record_not_found(
    client: AsyncClient, db: AsyncSession
) -> None:
    """会话存在但无病历 → RECORD_NOT_FOUND。"""
    session = await _create_session(db, stage="inquiry")
    try:
        resp = await client.get(
            f"/api/v1/consult/sessions/{session.id}/record",
        )
        assert resp.status_code == 404, resp.text
        assert resp.json()["code"] == "RECORD_NOT_FOUND"
    finally:
        pass


@pytest.mark.integration
@pytest.mark.asyncio(loop_scope="module")
async def test_get_record_invalid_version(
    client: AsyncClient, db: AsyncSession
) -> None:
    """非法 version 参数 → VALIDATION_ERROR。"""
    session = await _create_session(db, stage="record")
    review = await _create_doctor_review(db, session)
    await _create_medical_record(db, session, version=1, doctor_review_id=review.id)
    try:
        resp = await client.get(
            f"/api/v1/consult/sessions/{session.id}/record?version=invalid",
        )
        assert resp.status_code == 422, resp.text
        body = resp.json()
        assert body["code"] == "VALIDATION_ERROR"
    finally:
        pass


# ---------------------------------------------------------------------------
# PUT /record
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio(loop_scope="module")
async def test_edit_record_success(
    client: AsyncClient, db: AsyncSession
) -> None:
    """编辑病历成功：新增 version=2，写入 audit record.edited。"""
    session = await _create_session(db, stage="done")
    review = await _create_doctor_review(db, session)
    await _create_medical_record(
        db, session, version=1, doctor_review_id=review.id,
        record_text="原文本",
        record_json={"chief_complaint": "头痛"},
    )
    try:
        resp = await client.put(
            f"/api/v1/consult/sessions/{session.id}/record",
            json={
                "record_text": "修改后的文本",
            },
            headers=_record_headers(state_version=1),
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        data = body["data"]
        assert data["version"] == 2
        assert data["edited_by_doctor"] is True
        assert data["diff_from_previous"] is not None
        assert "record_text" in data["diff_from_previous"].get("changed_fields", [])

        # 验证 DB 存在 version=2
        from sqlalchemy import select as sa_select
        stmt = sa_select(MedicalRecord).where(
            MedicalRecord.session_id == session.id,
            MedicalRecord.version == 2,
        )
        result = await db.execute(stmt)
        v2 = result.scalar_one_or_none()
        assert v2 is not None
        assert v2.record_text == "修改后的文本"
        assert v2.edited_by_doctor is True

        # 验证旧版本未变
        stmt_v1 = sa_select(MedicalRecord).where(
            MedicalRecord.session_id == session.id,
            MedicalRecord.version == 1,
        )
        result_v1 = await db.execute(stmt_v1)
        v1 = result_v1.scalar_one_or_none()
        assert v1 is not None
        assert v1.record_text == "原文本"

        # 验证 audit record.edited
        audit_count = await _count_audit_events(
            db, str(session.id), "record.edited"
        )
        assert audit_count == 1
    finally:
        pass


@pytest.mark.integration
@pytest.mark.asyncio(loop_scope="module")
async def test_edit_record_multiple_versions(
    client: AsyncClient, db: AsyncSession
) -> None:
    """多次编辑产生 version=2, 3, 4..."""
    session = await _create_session(db, stage="done")
    review = await _create_doctor_review(db, session)
    await _create_medical_record(
        db, session, version=1, doctor_review_id=review.id,
        record_text="v1",
    )
    try:
        # edit 1 → v2
        resp1 = await client.put(
            f"/api/v1/consult/sessions/{session.id}/record",
            json={"record_text": "v2"},
            headers=_record_headers(state_version=1),
        )
        assert resp1.status_code == 200
        assert resp1.json()["data"]["version"] == 2

        # edit 2 → v3
        resp2 = await client.put(
            f"/api/v1/consult/sessions/{session.id}/record",
            json={"record_text": "v3"},
            headers=_record_headers(state_version=2),
        )
        assert resp2.status_code == 200
        assert resp2.json()["data"]["version"] == 3

        # edit 3 → v4
        resp3 = await client.put(
            f"/api/v1/consult/sessions/{session.id}/record",
            json={"record_text": "v4"},
            headers=_record_headers(state_version=3),
        )
        assert resp3.status_code == 200
        assert resp3.json()["data"]["version"] == 4

        # 验证所有版本都存在
        from sqlalchemy import func as sqlfunc
        from sqlalchemy import select as sa_select

        count_stmt = (
            sa_select(sqlfunc.count())
            .select_from(MedicalRecord)
            .where(MedicalRecord.session_id == session.id)
        )
        result = await db.execute(count_stmt)
        count = result.scalar_one()
        assert count == 4

        # 验证 audit 有 3 条 record.edited
        audit_count = await _count_audit_events(
            db, str(session.id), "record.edited"
        )
        assert audit_count == 3
    finally:
        pass


@pytest.mark.integration
@pytest.mark.asyncio(loop_scope="module")
async def test_edit_record_json_only(
    client: AsyncClient, db: AsyncSession
) -> None:
    """仅编辑 record_json，text 不变。"""
    session = await _create_session(db, stage="done")
    review = await _create_doctor_review(db, session)
    await _create_medical_record(
        db, session, version=1, doctor_review_id=review.id,
        record_text="原文",
        record_json={"chief_complaint": "头痛"},
    )
    try:
        resp = await client.put(
            f"/api/v1/consult/sessions/{session.id}/record",
            json={
                "record_json": {"chief_complaint": "头痛加重", "syndrome": "风寒"},
            },
            headers=_record_headers(state_version=1),
        )
        assert resp.status_code == 200, resp.text
        data = resp.json()["data"]
        assert data["version"] == 2
        assert data["edited_by_doctor"] is True

        # 验证 v2 text 保持不变，json 已更新
        from sqlalchemy import select as sa_select
        stmt = sa_select(MedicalRecord).where(
            MedicalRecord.session_id == session.id,
            MedicalRecord.version == 2,
        )
        result = await db.execute(stmt)
        v2 = result.scalar_one_or_none()
        assert v2 is not None
        assert v2.record_text == "原文"
        assert v2.record_json["chief_complaint"] == "头痛加重"
        assert v2.record_json["syndrome"] == "风寒"
    finally:
        pass


@pytest.mark.integration
@pytest.mark.asyncio(loop_scope="module")
async def test_edit_record_invalid_stage(
    client: AsyncClient, db: AsyncSession
) -> None:
    """非 record/done 阶段 → INVALID_STAGE_TRANSITION。"""
    session = await _create_session(db, stage="inquiry")
    review = await _create_doctor_review(db, session)
    await _create_medical_record(
        db, session, version=1, doctor_review_id=review.id,
    )
    try:
        resp = await client.put(
            f"/api/v1/consult/sessions/{session.id}/record",
            json={"record_text": "xxx"},
            headers=_record_headers(state_version=1),
        )
        assert resp.status_code == 409, resp.text
        assert resp.json()["code"] == "INVALID_STAGE_TRANSITION"
    finally:
        pass


@pytest.mark.integration
@pytest.mark.asyncio(loop_scope="module")
async def test_edit_record_state_version_conflict(
    client: AsyncClient, db: AsyncSession
) -> None:
    """state_version 冲突 → INVALID_STATE_VERSION。"""
    session = await _create_session(db, stage="done")
    review = await _create_doctor_review(db, session)
    await _create_medical_record(
        db, session, version=1, doctor_review_id=review.id,
    )
    try:
        resp = await client.put(
            f"/api/v1/consult/sessions/{session.id}/record",
            json={"record_text": "xxx"},
            headers=_record_headers(state_version=999),
        )
        assert resp.status_code == 409, resp.text
        assert resp.json()["code"] == "INVALID_STATE_VERSION"
    finally:
        pass


@pytest.mark.integration
@pytest.mark.asyncio(loop_scope="module")
async def test_edit_record_no_existing_record(
    client: AsyncClient, db: AsyncSession
) -> None:
    """无已有病历 → RECORD_NOT_FOUND。"""
    session = await _create_session(db, stage="done")
    try:
        resp = await client.put(
            f"/api/v1/consult/sessions/{session.id}/record",
            json={"record_text": "xxx"},
            headers=_record_headers(state_version=1),
        )
        assert resp.status_code == 404, resp.text
        assert resp.json()["code"] == "RECORD_NOT_FOUND"
    finally:
        pass


@pytest.mark.integration
@pytest.mark.asyncio(loop_scope="module")
async def test_edit_record_session_not_found(client: AsyncClient) -> None:
    """会话不存在 → SESSION_NOT_FOUND。"""
    fake_id = uuid.uuid4()
    resp = await client.put(
        f"/api/v1/consult/sessions/{fake_id}/record",
        json={"record_text": "xxx"},
        headers=_record_headers(),
    )
    assert resp.status_code == 404, resp.text
    assert resp.json()["code"] == "SESSION_NOT_FOUND"


@pytest.mark.integration
@pytest.mark.asyncio(loop_scope="module")
async def test_edit_record_empty_body(
    client: AsyncClient, db: AsyncSession
) -> None:
    """空请求体（record_text 和 record_json 都为 None）→ VALIDATION_ERROR。"""
    session = await _create_session(db, stage="done")
    try:
        resp = await client.put(
            f"/api/v1/consult/sessions/{session.id}/record",
            json={},
            headers=_record_headers(state_version=1),
        )
        assert resp.status_code == 422, resp.text
        body = resp.json()
        assert body["code"] == "VALIDATION_ERROR"
    finally:
        pass


@pytest.mark.integration
@pytest.mark.asyncio(loop_scope="module")
async def test_edit_record_session_lock_conflict(
    client: AsyncClient, db: AsyncSession
) -> None:
    """会话锁冲突 → SESSION_BUSY。"""
    session = await _create_session(db, stage="done")
    review = await _create_doctor_review(db, session)
    await _create_medical_record(
        db, session, version=1, doctor_review_id=review.id,
    )
    redis = None
    try:
        try:
            from redis.asyncio import Redis

            from app.core.config import get_settings

            settings = get_settings()
            redis = Redis.from_url(settings.redis_url, decode_responses=True)
            await redis.ping()
            lock_key = f"xuanhu:session_lock:{session.id}"
            await redis.set(lock_key, "other-trace", nx=True, ex=90)
        except Exception as exc:  # noqa: BLE001
            pytest.fail(f"Redis integration dependency unavailable: {type(exc).__name__}: {exc}")

        resp = await client.put(
            f"/api/v1/consult/sessions/{session.id}/record",
            json={"record_text": "xxx"},
            headers=_record_headers(state_version=1),
        )
        assert resp.status_code == 409, resp.text
        assert resp.json()["code"] == "SESSION_BUSY"
    finally:
        if redis is not None:
            with contextlib.suppress(Exception):
                await redis.delete(f"xuanhu:session_lock:{session.id}")
            await redis.aclose()


# ---------------------------------------------------------------------------
# GET /record/export
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio(loop_scope="module")
async def test_export_txt(
    client: AsyncClient, db: AsyncSession
) -> None:
    """导出 txt 格式。"""
    session = await _create_session(db, stage="done")
    review = await _create_doctor_review(db, session)
    await _create_medical_record(
        db, session, version=1, doctor_review_id=review.id,
        record_text="【主诉】头痛3天",
    )
    try:
        resp = await client.get(
            f"/api/v1/consult/sessions/{session.id}/record/export?format=txt",
        )
        assert resp.status_code == 200, resp.text
        assert "text/plain" in resp.headers.get("content-type", "")
        assert "attachment" in resp.headers.get("content-disposition", "")
        assert "filename" in resp.headers["content-disposition"]
        assert "filename*" in resp.headers["content-disposition"]
        assert resp.text == "【主诉】头痛3天"

        # 验证 audit record.exported
        audit_count = await _count_audit_events(
            db, str(session.id), "record.exported"
        )
        assert audit_count == 1
    finally:
        pass


@pytest.mark.integration
@pytest.mark.asyncio(loop_scope="module")
async def test_export_json(
    client: AsyncClient, db: AsyncSession
) -> None:
    """导出 json 格式。"""
    session = await _create_session(db, stage="done")
    review = await _create_doctor_review(db, session)
    await _create_medical_record(
        db, session, version=1, doctor_review_id=review.id,
        record_json={"chief_complaint": "头痛"},
    )
    try:
        resp = await client.get(
            f"/api/v1/consult/sessions/{session.id}/record/export?format=json",
        )
        assert resp.status_code == 200, resp.text
        assert "application/json" in resp.headers.get("content-type", "")
        assert "attachment" in resp.headers.get("content-disposition", "")
        content = resp.json()
        assert content["chief_complaint"] == "头痛"
        assert content["record_id"] == str(
            (await _get_latest_record(db, session)).id
        )
    finally:
        pass


@pytest.mark.integration
@pytest.mark.asyncio(loop_scope="module")
async def test_export_md(
    client: AsyncClient, db: AsyncSession
) -> None:
    """导出 md 格式。"""
    session = await _create_session(db, stage="done")
    review = await _create_doctor_review(db, session)
    await _create_medical_record(
        db, session, version=1, doctor_review_id=review.id,
        record_json={
            "chief_complaint": "头痛3天",
            "syndrome": "风寒束表证",
            "treatment_principle": "辛温解表",
        },
    )
    try:
        resp = await client.get(
            f"/api/v1/consult/sessions/{session.id}/record/export?format=md",
        )
        assert resp.status_code == 200, resp.text
        assert "text/markdown" in resp.headers.get("content-type", "")
        assert "attachment" in resp.headers.get("content-disposition", "")
        content = resp.text
        assert "# 病历记录" in content
        assert "头痛3天" in content
        assert "风寒束表证" in content
    finally:
        pass


@pytest.mark.integration
@pytest.mark.asyncio(loop_scope="module")
async def test_export_unsupported_format(
    client: AsyncClient, db: AsyncSession
) -> None:
    """不支持的格式 → EXPORT_FORMAT_UNSUPPORTED。"""
    session = await _create_session(db, stage="done")
    review = await _create_doctor_review(db, session)
    await _create_medical_record(
        db, session, version=1, doctor_review_id=review.id,
    )
    try:
        resp = await client.get(
            f"/api/v1/consult/sessions/{session.id}/record/export?format=docx",
        )
        assert resp.status_code == 400, resp.text
        assert resp.json()["code"] == "EXPORT_FORMAT_UNSUPPORTED"
    finally:
        pass


@pytest.mark.integration
@pytest.mark.asyncio(loop_scope="module")
async def test_export_no_record(
    client: AsyncClient, db: AsyncSession
) -> None:
    """无病历导出 → RECORD_NOT_FOUND。"""
    session = await _create_session(db, stage="done")
    try:
        resp = await client.get(
            f"/api/v1/consult/sessions/{session.id}/record/export?format=txt",
        )
        assert resp.status_code == 404, resp.text
        assert resp.json()["code"] == "RECORD_NOT_FOUND"
    finally:
        pass


@pytest.mark.integration
@pytest.mark.asyncio(loop_scope="module")
async def test_export_session_not_found(client: AsyncClient) -> None:
    """会话不存在 → SESSION_NOT_FOUND。"""
    fake_id = uuid.uuid4()
    resp = await client.get(
        f"/api/v1/consult/sessions/{fake_id}/record/export?format=txt",
    )
    assert resp.status_code == 404, resp.text
    assert resp.json()["code"] == "SESSION_NOT_FOUND"


@pytest.mark.integration
@pytest.mark.asyncio(loop_scope="module")
async def test_export_with_version(
    client: AsyncClient, db: AsyncSession
) -> None:
    """导出指定版本。"""
    session = await _create_session(db, stage="done")
    review = await _create_doctor_review(db, session)
    await _create_medical_record(
        db, session, version=1, doctor_review_id=review.id,
        record_text="v1 text",
    )
    await _create_medical_record(
        db, session, version=2, doctor_review_id=review.id,
        record_text="v2 text",
        edited_by_doctor=True,
    )
    try:
        # 默认 latest = v2
        resp1 = await client.get(
            f"/api/v1/consult/sessions/{session.id}/record/export?format=txt",
        )
        assert resp1.text == "v2 text"

        # 指定 version=1
        resp2 = await client.get(
            f"/api/v1/consult/sessions/{session.id}/record/export?format=txt&version=1",
        )
        assert resp2.text == "v1 text"
    finally:
        pass


@pytest.mark.integration
@pytest.mark.asyncio(loop_scope="module")
async def test_export_content_disposition_has_chinese_filename(
    client: AsyncClient, db: AsyncSession
) -> None:
    """Content-Disposition 含 ASCII fallback 和 RFC 5987 filename*。"""
    session = await _create_session(db, stage="done")
    review = await _create_doctor_review(db, session)
    await _create_medical_record(
        db, session, version=1, doctor_review_id=review.id,
    )
    try:
        resp = await client.get(
            f"/api/v1/consult/sessions/{session.id}/record/export?format=txt",
        )
        cd = resp.headers["content-disposition"]
        assert "attachment" in cd
        assert 'filename="medical_record.txt"' in cd
        assert "filename*=UTF-8''" in cd
    finally:
        pass


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------


async def _get_latest_record(
    db: AsyncSession, session: ConsultSession
) -> MedicalRecord:
    """获取最新版本病历。"""
    stmt = (
        select(MedicalRecord)
        .where(MedicalRecord.session_id == session.id)
        .order_by(MedicalRecord.version.desc())
        .limit(1)
    )
    result = await db.execute(stmt)
    return result.scalar_one()
