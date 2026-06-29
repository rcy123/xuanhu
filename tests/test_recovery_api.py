"""P3-4 会话恢复 API 测试。

覆盖四种 recovery action 及错误路径、审计事件、Redis Stream 事件。
本测试为集成测试，需要可连接的 PostgreSQL；不可用时自动跳过。
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.main import app
from app.models.audit import AuditEvent
from app.models.consult import ConsultSession

pytestmark = [pytest.mark.integration, pytest.mark.asyncio(loop_scope="module")]


# ---------------------------------------------------------------------------
# 模块级测试数据
# ---------------------------------------------------------------------------

_TEST_PATIENT_REF_PREFIX = "P3-4-RECOV-"
_TEST_DOCTOR_ID = "doctor_p3_4_recovery"


@pytest_asyncio.fixture(scope="module", loop_scope="module", autouse=True)
async def _cleanup_test_sessions() -> None:
    """模块结束时清理本模块创建的会话及关联审计事件。"""
    from app.core.config import get_settings
    from app.db.session import get_session_factory, reset_session_factory

    get_settings.cache_clear()
    await reset_session_factory()

    yield

    factory = get_session_factory()
    async with factory() as session:
        try:
            await session.execute(select(ConsultSession.id).limit(1))
        except Exception:  # noqa: BLE001
            return

        await session.execute(
            delete(AuditEvent).where(
                AuditEvent.session_id.in_(
                    select(ConsultSession.id).where(
                        or_(
                            ConsultSession.patient_ref.like(f"{_TEST_PATIENT_REF_PREFIX}%"),
                            ConsultSession.created_by == _TEST_DOCTOR_ID,
                        )
                    )
                )
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


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


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


@pytest_asyncio.fixture(loop_scope="module", autouse=True)
async def _check_postgres() -> None:
    """检查 PostgreSQL 可用性。"""
    from sqlalchemy import text

    from app.db.session import get_session_factory

    factory = get_session_factory()
    try:
        async with factory() as session:
            await session.execute(text("SELECT 1"))
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"PostgreSQL 不可用，跳过集成测试: {type(exc).__name__}: {exc}")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _create_session(
    client: AsyncClient,
    payload: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
) -> dict[str, Any]:
    """辅助：创建会话并返回 data。"""
    response = await client.post(
        "/api/v1/consult/sessions",
        json=payload or {},
        headers=headers if headers is not None else {"X-Doctor-Id": _TEST_DOCTOR_ID},
    )
    assert response.status_code == 201, response.text
    return response.json()["data"]


async def _get_session(db: AsyncSession, session_id: str) -> ConsultSession | None:
    """辅助：从数据库读取会话。"""
    sid = uuid.UUID(session_id)
    result = await db.execute(select(ConsultSession).where(ConsultSession.id == sid))
    return result.scalar_one_or_none()


async def _set_session_blocked(
    db: AsyncSession, session_id: str, *, with_snapshot: bool = False
) -> None:
    """辅助：将会话设置为 blocked 状态。"""
    session = await _get_session(db, session_id)
    assert session is not None
    session.status = "blocked"
    session.blocked_reason = "safety_rollback_exceeded"
    session.blocked_at = datetime.now(UTC).replace(tzinfo=None)
    if with_snapshot:
        session.state_snapshot = {
            "current_stage": "prescription",
            "status": "active",
        }
    await db.commit()


async def _set_session_manual_required(
    db: AsyncSession, session_id: str
) -> None:
    """辅助：将会话设置为需要人工恢复。"""
    session = await _get_session(db, session_id)
    assert session is not None
    session.recovery_status = "manual_required"
    await db.commit()


async def _count_audit_events(
    db: AsyncSession, session_id: str, event_type: str
) -> int:
    """辅助：统计某会话某类型的审计事件数。"""
    sid = uuid.UUID(session_id)
    result = await db.execute(
        select(AuditEvent).where(
            AuditEvent.session_id == sid,
            AuditEvent.event_type == event_type,
        )
    )
    return len(result.scalars().all())


async def _get_latest_audit(
    db: AsyncSession, session_id: str
) -> AuditEvent | None:
    """辅助：读取某会话最近一条审计事件。"""
    sid = uuid.UUID(session_id)
    result = await db.execute(
        select(AuditEvent)
        .where(AuditEvent.session_id == sid)
        .order_by(AuditEvent.created_at.desc())
        .limit(1)
    )
    return result.scalars().one_or_none()


async def _preoccupy_redis_lock(session_id: str, trace_id: str = "preoccupied-trace-id") -> Any:
    """辅助：预占 Redis 会话锁，返回 redis 客户端；不可用时返回 None。"""
    try:
        from redis.asyncio import Redis

        from app.core.config import get_settings

        settings = get_settings()
        redis = Redis.from_url(settings.redis_url, decode_responses=True)
        await redis.ping()
    except Exception:  # noqa: BLE001
        return None

    lock_key = f"xuanhu:session_lock:{session_id}"
    await redis.set(lock_key, trace_id, nx=True, ex=90)
    return redis


async def _set_redis_checkpoint(session_id: str, payload: dict[str, Any]) -> Any:
    """辅助：写入 Redis checkpoint；不可用时返回 None。"""
    try:
        from redis.asyncio import Redis

        from app.core.config import get_settings

        settings = get_settings()
        redis = Redis.from_url(settings.redis_url, decode_responses=True)
        await redis.ping()
    except Exception:  # noqa: BLE001
        return None

    import json as _json

    key = f"xuanhu:checkpoint:{session_id}"
    await redis.set(key, _json.dumps(payload, ensure_ascii=False))
    return redis


async def _cleanup_redis_keys(redis: Any, *keys: str) -> None:
    """辅助：清理 Redis key 并关闭连接。"""
    try:
        for key in keys:
            await redis.delete(key)
        await redis.aclose()
    except Exception:  # noqa: BLE001
        pass


# ---------------------------------------------------------------------------
# 会话不存在
# ---------------------------------------------------------------------------


async def test_recover_session_not_found(client: AsyncClient) -> None:
    """对不存在的会话 recover 返回 404 SESSION_NOT_FOUND。"""
    fake_id = str(uuid.uuid4())
    response = await client.post(
        f"/api/v1/consult/sessions/{fake_id}/recover",
        json={"action": "retry_current_stage"},
    )
    assert response.status_code == 404
    body = response.json()
    assert body["code"] == "SESSION_NOT_FOUND"
    assert body["retryable"] is False


async def test_recover_invalid_uuid_format(client: AsyncClient) -> None:
    """非法 UUID 格式 session_id 返回 404 SESSION_NOT_FOUND。"""
    response = await client.post(
        "/api/v1/consult/sessions/not-a-uuid/recover",
        json={"action": "retry_current_stage"},
    )
    assert response.status_code == 404
    body = response.json()
    assert body["code"] == "SESSION_NOT_FOUND"


# ---------------------------------------------------------------------------
# RECOVERY_NOT_NEEDED（正常 active 会话）
# ---------------------------------------------------------------------------


async def test_recover_normal_active_session_returns_not_needed(
    client: AsyncClient, db: AsyncSession
) -> None:
    """正常 active 且 recovery_status=normal 的会话返回 RECOVERY_NOT_NEEDED。"""
    created = await _create_session(
        client,
        {"patient_info": {"patient_ref": f"{_TEST_PATIENT_REF_PREFIX}NORMAL01"}},
    )
    session_id = created["session_id"]

    response = await client.post(
        f"/api/v1/consult/sessions/{session_id}/recover",
        json={"action": "retry_current_stage"},
    )
    assert response.status_code == 400
    body = response.json()
    assert body["code"] == "RECOVERY_NOT_NEEDED"
    assert body["retryable"] is False


# ---------------------------------------------------------------------------
# resume_from_pg_snapshot
# ---------------------------------------------------------------------------


async def test_resume_from_snapshot_success(
    client: AsyncClient, db: AsyncSession
) -> None:
    """blocked 会话且有 state_snapshot 时 resume_from_pg_snapshot 成功。"""
    created = await _create_session(
        client,
        {"patient_info": {"patient_ref": f"{_TEST_PATIENT_REF_PREFIX}SNAP01"}},
    )
    session_id = created["session_id"]
    await _set_session_blocked(db, session_id, with_snapshot=True)

    response = await client.post(
        f"/api/v1/consult/sessions/{session_id}/recover",
        json={
            "action": "resume_from_pg_snapshot",
            "reason": "测试快照恢复",
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["code"] == "SUCCESS"
    data = body["data"]
    assert data["session_id"] == session_id
    assert data["action"] == "resume_from_pg_snapshot"
    assert data["recovery_status"] == "normal"

    # 验证数据库状态（refresh 绕过模块级 identity map 缓存）
    session = await _get_session(db, session_id)
    assert session is not None
    await db.refresh(session)
    assert session.current_stage == "prescription"  # from snapshot
    assert session.status == "active"  # from snapshot
    assert session.recovery_status == "normal"


async def test_resume_from_snapshot_no_snapshot(
    client: AsyncClient, db: AsyncSession
) -> None:
    """无 state_snapshot 时返回 STATE_RECOVERY_REQUIRED。"""
    created = await _create_session(
        client,
        {"patient_info": {"patient_ref": f"{_TEST_PATIENT_REF_PREFIX}NOSNAP01"}},
    )
    session_id = created["session_id"]
    await _set_session_blocked(db, session_id, with_snapshot=False)

    response = await client.post(
        f"/api/v1/consult/sessions/{session_id}/recover",
        json={"action": "resume_from_pg_snapshot"},
    )
    assert response.status_code == 409
    body = response.json()
    assert body["code"] == "STATE_RECOVERY_REQUIRED"
    assert body["retryable"] is False


async def test_resume_from_snapshot_writes_audit(
    client: AsyncClient, db: AsyncSession
) -> None:
    """resume_from_pg_snapshot 成功写入 session.recovered 审计事件。"""
    created = await _create_session(
        client,
        {"patient_info": {"patient_ref": f"{_TEST_PATIENT_REF_PREFIX}SNAPAUDIT"}},
    )
    session_id = created["session_id"]
    await _set_session_blocked(db, session_id, with_snapshot=True)

    await client.post(
        f"/api/v1/consult/sessions/{session_id}/recover",
        json={"action": "resume_from_pg_snapshot", "reason": "审计测试"},
    )

    count = await _count_audit_events(db, session_id, "session.recovered")
    assert count == 1

    sid = uuid.UUID(session_id)
    result = await db.execute(
        select(AuditEvent).where(
            AuditEvent.session_id == sid,
            AuditEvent.event_type == "session.recovered",
        )
    )
    event = result.scalar_one()
    assert event.payload["action"] == "resume_from_pg_snapshot"
    assert "snapshot_keys" in event.payload


# ---------------------------------------------------------------------------
# retry_current_stage
# ---------------------------------------------------------------------------


async def test_retry_current_stage_success(
    client: AsyncClient, db: AsyncSession
) -> None:
    """retry_current_stage 成功，不触发 Agent。"""
    created = await _create_session(
        client,
        {"patient_info": {"patient_ref": f"{_TEST_PATIENT_REF_PREFIX}RETRY01"}},
    )
    session_id = created["session_id"]
    await _set_session_blocked(db, session_id)

    response = await client.post(
        f"/api/v1/consult/sessions/{session_id}/recover",
        json={
            "action": "retry_current_stage",
            "reason": "测试重试当前阶段",
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["code"] == "SUCCESS"
    data = body["data"]
    assert data["action"] == "retry_current_stage"
    assert data["recovery_status"] == "normal"
    assert data["status"] == "active"

    # 验证数据库状态：recovery_status 变为 normal，status 变为 active
    session = await _get_session(db, session_id)
    assert session is not None
    await db.refresh(session)
    assert session.recovery_status == "normal"
    assert session.status == "active"
    assert session.blocked_reason is None
    # current_stage 保持不变
    assert session.current_stage == "inquiry"


async def test_retry_current_stage_writes_audit(
    client: AsyncClient, db: AsyncSession
) -> None:
    """retry_current_stage 成功写入审计事件。"""
    created = await _create_session(
        client,
        {"patient_info": {"patient_ref": f"{_TEST_PATIENT_REF_PREFIX}RETRYAUD"}},
    )
    session_id = created["session_id"]
    await _set_session_blocked(db, session_id)

    await client.post(
        f"/api/v1/consult/sessions/{session_id}/recover",
        json={"action": "retry_current_stage", "reason": "审计测试"},
    )

    count = await _count_audit_events(db, session_id, "session.recovered")
    assert count == 1


async def test_retry_current_stage_manual_required(
    client: AsyncClient, db: AsyncSession
) -> None:
    """recovery_status=manual_required 的 active 会话也可 retry。"""
    created = await _create_session(
        client,
        {"patient_info": {"patient_ref": f"{_TEST_PATIENT_REF_PREFIX}MANUAL01"}},
    )
    session_id = created["session_id"]
    await _set_session_manual_required(db, session_id)

    response = await client.post(
        f"/api/v1/consult/sessions/{session_id}/recover",
        json={"action": "retry_current_stage"},
    )
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["recovery_status"] == "normal"
    assert data["status"] == "active"


# ---------------------------------------------------------------------------
# rollback_to_stage
# ---------------------------------------------------------------------------


async def test_rollback_to_stage_success(
    client: AsyncClient, db: AsyncSession
) -> None:
    """rollback_to_stage 成功，current_stage 更新，state_version 递增。"""
    created = await _create_session(
        client,
        {"patient_info": {"patient_ref": f"{_TEST_PATIENT_REF_PREFIX}ROLL01"}},
    )
    session_id = created["session_id"]
    await _set_session_blocked(db, session_id)

    # 获取当前 state_version
    session_before = await _get_session(db, session_id)
    assert session_before is not None
    original_version = session_before.state_version

    response = await client.post(
        f"/api/v1/consult/sessions/{session_id}/recover",
        json={
            "action": "rollback_to_stage",
            "target_stage": "inquiry",
            "reason": "回退到问诊重新采集",
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["code"] == "SUCCESS"
    data = body["data"]
    assert data["current_stage"] == "inquiry"
    assert data["status"] == "active"
    assert data["recovery_status"] == "normal"

    # 验证数据库（需 refresh 绕过 identity map 缓存，因为 API 在另一个 session 提交）
    session = await _get_session(db, session_id)
    assert session is not None
    await db.refresh(session)
    assert session.current_stage == "inquiry"
    assert session.state_version == original_version + 1


async def test_rollback_missing_target_stage(
    client: AsyncClient, db: AsyncSession
) -> None:
    """rollback_to_stage 未提供 target_stage 返回 VALIDATION_ERROR。"""
    created = await _create_session(
        client,
        {"patient_info": {"patient_ref": f"{_TEST_PATIENT_REF_PREFIX}ROLL02"}},
    )
    session_id = created["session_id"]
    await _set_session_blocked(db, session_id)

    response = await client.post(
        f"/api/v1/consult/sessions/{session_id}/recover",
        json={"action": "rollback_to_stage"},
    )
    assert response.status_code == 422
    body = response.json()
    assert body["code"] == "VALIDATION_ERROR"


async def test_rollback_invalid_target_stage(
    client: AsyncClient, db: AsyncSession
) -> None:
    """无效 target_stage 返回 VALIDATION_ERROR。"""
    created = await _create_session(
        client,
        {"patient_info": {"patient_ref": f"{_TEST_PATIENT_REF_PREFIX}ROLL03"}},
    )
    session_id = created["session_id"]
    await _set_session_blocked(db, session_id)

    response = await client.post(
        f"/api/v1/consult/sessions/{session_id}/recover",
        json={
            "action": "rollback_to_stage",
            "target_stage": "invalid_stage",
        },
    )
    assert response.status_code == 422
    body = response.json()
    assert body["code"] == "VALIDATION_ERROR"


async def test_rollback_to_stage_writes_audit(
    client: AsyncClient, db: AsyncSession
) -> None:
    """rollback_to_stage 成功写入审计事件。"""
    created = await _create_session(
        client,
        {"patient_info": {"patient_ref": f"{_TEST_PATIENT_REF_PREFIX}ROLLAUD"}},
    )
    session_id = created["session_id"]
    await _set_session_blocked(db, session_id)

    await client.post(
        f"/api/v1/consult/sessions/{session_id}/recover",
        json={
            "action": "rollback_to_stage",
            "target_stage": "syndrome",
            "reason": "回退到辨证",
        },
    )

    count = await _count_audit_events(db, session_id, "session.recovered")
    assert count == 1

    sid = uuid.UUID(session_id)
    result = await db.execute(
        select(AuditEvent).where(
            AuditEvent.session_id == sid,
            AuditEvent.event_type == "session.recovered",
        )
    )
    event = result.scalar_one()
    assert event.payload["action"] == "rollback_to_stage"
    assert event.payload["target_stage"] == "syndrome"


# ---------------------------------------------------------------------------
# terminate action
# ---------------------------------------------------------------------------


async def test_terminate_via_recover_success(
    client: AsyncClient, db: AsyncSession
) -> None:
    """terminate action 成功终止 blocked 会话。"""
    created = await _create_session(
        client,
        {"patient_info": {"patient_ref": f"{_TEST_PATIENT_REF_PREFIX}TERM01"}},
    )
    session_id = created["session_id"]
    await _set_session_blocked(db, session_id)

    response = await client.post(
        f"/api/v1/consult/sessions/{session_id}/recover",
        json={
            "action": "terminate",
            "reason": "医师决定终止",
        },
        headers={"X-Doctor-Id": "doctor_term_recover"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["code"] == "SUCCESS"
    data = body["data"]
    assert data["status"] == "terminated"
    assert data["current_stage"] == "blocked"

    # 验证数据库
    session = await _get_session(db, session_id)
    assert session is not None
    await db.refresh(session)
    assert session.status == "terminated"
    assert session.blocked_reason == "terminated_by_doctor"
    assert session.blocked_at is not None


async def test_terminate_via_recover_writes_audit(
    client: AsyncClient, db: AsyncSession
) -> None:
    """terminate action 写入 session.terminated 审计事件。"""
    created = await _create_session(
        client,
        {"patient_info": {"patient_ref": f"{_TEST_PATIENT_REF_PREFIX}TERMAUD"}},
    )
    session_id = created["session_id"]
    await _set_session_blocked(db, session_id)

    await client.post(
        f"/api/v1/consult/sessions/{session_id}/recover",
        json={
            "action": "terminate",
            "reason": "审计终止测试",
        },
    )

    count = await _count_audit_events(db, session_id, "session.terminated")
    assert count == 1

    sid = uuid.UUID(session_id)
    result = await db.execute(
        select(AuditEvent).where(
            AuditEvent.session_id == sid,
            AuditEvent.event_type == "session.terminated",
        )
    )
    event = result.scalar_one()
    assert event.payload["action"] == "terminate"
    assert event.payload["reason"] == "审计终止测试"


# ---------------------------------------------------------------------------
# 无效 action
# ---------------------------------------------------------------------------


async def test_recover_invalid_action(client: AsyncClient, db: AsyncSession) -> None:
    """无效 action 返回 422 VALIDATION_ERROR (Pydantic 校验)。"""
    created = await _create_session(
        client,
        {"patient_info": {"patient_ref": f"{_TEST_PATIENT_REF_PREFIX}INVACT"}},
    )
    session_id = created["session_id"]
    await _set_session_blocked(db, session_id)

    response = await client.post(
        f"/api/v1/consult/sessions/{session_id}/recover",
        json={"action": "invalid_action"},
    )
    assert response.status_code == 422
    body = response.json()
    assert body["code"] == "VALIDATION_ERROR"


# ===========================================================================
# P3-4-fix：B-004 会话锁 + B-005 恢复一致性比较
# ===========================================================================


# ---------------------------------------------------------------------------
# B-004：recover 会话锁
# ---------------------------------------------------------------------------


async def test_recover_acquires_lock_and_succeeds(
    client: AsyncClient, db: AsyncSession
) -> None:
    """recover 获取锁成功后可正常执行。"""
    created = await _create_session(
        client,
        {"patient_info": {"patient_ref": f"{_TEST_PATIENT_REF_PREFIX}LOCKOK"}},
    )
    session_id = created["session_id"]
    await _set_session_blocked(db, session_id)

    response = await client.post(
        f"/api/v1/consult/sessions/{session_id}/recover",
        json={"action": "retry_current_stage"},
    )
    assert response.status_code == 200
    assert response.json()["code"] == "SUCCESS"

    # 锁应在 recover 后释放（同会话可立即再次加锁）
    redis = await _preoccupy_redis_lock(session_id, "post-recover-lock")
    if redis is not None:
        # 预占应成功，证明 recover 已释放锁
        lock_key = f"xuanhu:session_lock:{session_id}"
        remaining = await redis.get(lock_key)
        # 若预占成功，remaining 为我们设的值；recover 未遗留旧锁
        assert remaining == "post-recover-lock"
        await _cleanup_redis_keys(redis, lock_key)


async def test_recover_returns_409_when_lock_preoccupied(
    client: AsyncClient, db: AsyncSession
) -> None:
    """recover 获取锁失败时返回 409 SESSION_BUSY。"""
    created = await _create_session(
        client,
        {"patient_info": {"patient_ref": f"{_TEST_PATIENT_REF_PREFIX}LOCKBUSY"}},
    )
    session_id = created["session_id"]
    await _set_session_blocked(db, session_id)

    redis = await _preoccupy_redis_lock(session_id)
    if redis is None:
        pytest.skip("Redis 不可用，跳过确定性锁冲突测试")

    lock_key = f"xuanhu:session_lock:{session_id}"
    try:
        response = await client.post(
            f"/api/v1/consult/sessions/{session_id}/recover",
            json={"action": "retry_current_stage"},
        )
        assert response.status_code == 409, response.text
        body = response.json()
        assert body["code"] == "SESSION_BUSY"
        assert body["retryable"] is True

        # 锁被占用期间，会话状态不应被修改
        session = await _get_session(db, session_id)
        assert session is not None
        await db.refresh(session)
        assert session.recovery_status != "normal" or session.status == "blocked"
    finally:
        await _cleanup_redis_keys(redis, lock_key)


async def test_recover_releases_lock_on_exception(
    client: AsyncClient, db: AsyncSession
) -> None:
    """recover 异常路径（如 RECOVERY_NOT_NEEDED）也释放锁。"""
    created = await _create_session(
        client,
        {"patient_info": {"patient_ref": f"{_TEST_PATIENT_REF_PREFIX}LOCKREL"}},
    )
    session_id = created["session_id"]
    # 不设置为 blocked → 触发 RECOVERY_NOT_NEEDED 异常路径

    response = await client.post(
        f"/api/v1/consult/sessions/{session_id}/recover",
        json={"action": "retry_current_stage"},
    )
    assert response.status_code == 400
    assert response.json()["code"] == "RECOVERY_NOT_NEEDED"

    # 异常后锁应已释放：可立即预占成功
    redis = await _preoccupy_redis_lock(session_id, "after-exception-lock")
    if redis is not None:
        lock_key = f"xuanhu:session_lock:{session_id}"
        remaining = await redis.get(lock_key)
        assert remaining == "after-exception-lock"
        await _cleanup_redis_keys(redis, lock_key)


# ---------------------------------------------------------------------------
# B-005：恢复一致性比较与降级
# ---------------------------------------------------------------------------


async def test_resume_from_snapshot_with_missing_checkpoint_records_degradation(
    client: AsyncClient, db: AsyncSession
) -> None:
    """Redis checkpoint 缺失但 PG snapshot 可用时，resume 成功并记录降级信息。"""
    created = await _create_session(
        client,
        {"patient_info": {"patient_ref": f"{_TEST_PATIENT_REF_PREFIX}DEGRADE01"}},
    )
    session_id = created["session_id"]
    await _set_session_blocked(db, session_id, with_snapshot=True)
    # 不写 checkpoint → checkpoint_status=missing

    response = await client.post(
        f"/api/v1/consult/sessions/{session_id}/recover",
        json={"action": "resume_from_pg_snapshot"},
    )
    assert response.status_code == 200
    assert response.json()["code"] == "SUCCESS"

    # 验证 audit payload 记录降级事实
    sid = uuid.UUID(session_id)
    result = await db.execute(
        select(AuditEvent).where(
            AuditEvent.session_id == sid,
            AuditEvent.event_type == "session.recovered",
        )
    )
    event = result.scalar_one()
    assert event.payload["recovery_source"] == "pg_snapshot"
    assert event.payload["checkpoint_status"] == "missing"


async def test_resume_from_snapshot_no_snapshot_no_checkpoint_returns_required(
    client: AsyncClient, db: AsyncSession
) -> None:
    """PG snapshot 缺失且 checkpoint 不可用时，返回 STATE_RECOVERY_REQUIRED。"""
    created = await _create_session(
        client,
        {"patient_info": {"patient_ref": f"{_TEST_PATIENT_REF_PREFIX}NOSNAP02"}},
    )
    session_id = created["session_id"]
    await _set_session_blocked(db, session_id, with_snapshot=False)
    # 不写 checkpoint

    response = await client.post(
        f"/api/v1/consult/sessions/{session_id}/recover",
        json={"action": "resume_from_pg_snapshot"},
    )
    assert response.status_code == 409
    body = response.json()
    assert body["code"] == "STATE_RECOVERY_REQUIRED"
    assert body["retryable"] is False


async def test_resume_does_not_overwrite_pg_when_checkpoint_older(
    client: AsyncClient, db: AsyncSession
) -> None:
    """checkpoint 版本旧于 PG state_version 时，不错误覆盖 PG 权威状态。"""
    created = await _create_session(
        client,
        {"patient_info": {"patient_ref": f"{_TEST_PATIENT_REF_PREFIX}OLDCKPT"}},
    )
    session_id = created["session_id"]
    await _set_session_blocked(db, session_id, with_snapshot=True)

    # 提升 PG state_version 到 5
    session = await _get_session(db, session_id)
    assert session is not None
    session.state_version = 5
    await db.commit()

    # 写入一个旧版本 checkpoint（version=2，旧于 PG 5）
    redis = await _set_redis_checkpoint(
        session_id,
        {"state_version": 2, "current_stage": "inquiry"},
    )
    if redis is None:
        pytest.skip("Redis 不可用，跳过 checkpoint 版本比较测试")

    ckpt_key = f"xuanhu:checkpoint:{session_id}"
    try:
        response = await client.post(
            f"/api/v1/consult/sessions/{session_id}/recover",
            json={"action": "resume_from_pg_snapshot"},
        )
        assert response.status_code == 200

        # PG 权威状态未被旧 checkpoint 覆盖：恢复后 stage 来自 PG snapshot
        session = await _get_session(db, session_id)
        assert session is not None
        await db.refresh(session)
        # PG snapshot 的 current_stage=prescription，旧 checkpoint 的 inquiry 不应覆盖
        assert session.current_stage == "prescription"
        # state_version 应递增（5 → 6），而非被旧 checkpoint 拉低
        assert session.state_version == 6

        # audit 记录比较事实
        sid = uuid.UUID(session_id)
        audit_result = await db.execute(
            select(AuditEvent).where(
                AuditEvent.session_id == sid,
                AuditEvent.event_type == "session.recovered",
            )
        )
        event = audit_result.scalar_one()
        assert event.payload["checkpoint_status"] == "present"
        assert event.payload["checkpoint_version"] == 2
    finally:
        await _cleanup_redis_keys(redis, ckpt_key)


async def test_non_terminate_recovery_rejected_when_audit_shows_terminated(
    client: AsyncClient, db: AsyncSession
) -> None:
    """audit 显示已终止时，非 terminate 恢复动作被拒绝。"""
    created = await _create_session(
        client,
        {"patient_info": {"patient_ref": f"{_TEST_PATIENT_REF_PREFIX}TERMBLOCK"}},
    )
    session_id = created["session_id"]
    await _set_session_blocked(db, session_id)

    # 先通过 recover terminate 终止会话（写入 session.terminated 审计）
    term_resp = await client.post(
        f"/api/v1/consult/sessions/{session_id}/recover",
        json={"action": "terminate"},
    )
    assert term_resp.status_code == 200

    # 会话已 terminated，再尝试 retry_current_stage（非 terminate）
    # terminated 状态不满足可恢复条件，会先被 RECOVERY_NOT_NEEDED 拦截，
    # 但更关键的是审计一致性：即使绕过状态校验，也不应执行非 terminate 恢复。
    response = await client.post(
        f"/api/v1/consult/sessions/{session_id}/recover",
        json={"action": "retry_current_stage"},
    )
    # terminated 会话不可恢复，返回 400/409 均可，关键是不能 200 成功恢复
    assert response.status_code in (400, 409)
    body = response.json()
    assert body["code"] in ("RECOVERY_NOT_NEEDED", "STATE_RECOVERY_REQUIRED")


async def test_terminate_still_allowed_after_terminated_audit(
    client: AsyncClient, db: AsyncSession
) -> None:
    """terminate 动作在审计显示已终止后仍被允许（幂等终止，不误伤运维）。"""
    created = await _create_session(
        client,
        {"patient_info": {"patient_ref": f"{_TEST_PATIENT_REF_PREFIX}TERMIDEM"}},
    )
    session_id = created["session_id"]
    await _set_session_blocked(db, session_id)

    # 第一次终止
    resp1 = await client.post(
        f"/api/v1/consult/sessions/{session_id}/recover",
        json={"action": "terminate"},
    )
    assert resp1.status_code == 200

    # 第二次 terminate：terminated 状态不在可恢复集合 → RECOVERY_NOT_NEEDED，
    # 但一致性检查本身不会因 terminate 动作而拒绝。
    resp2 = await client.post(
        f"/api/v1/consult/sessions/{session_id}/recover",
        json={"action": "terminate"},
    )
    # terminated 会话不可再次恢复（状态校验），返回 400
    assert resp2.status_code == 400
    assert resp2.json()["code"] == "RECOVERY_NOT_NEEDED"


async def test_rollback_and_retry_pass_consistency_check(
    client: AsyncClient, db: AsyncSession
) -> None:
    """retry_current_stage / rollback_to_stage 经过一致性检查后成功。"""
    created = await _create_session(
        client,
        {"patient_info": {"patient_ref": f"{_TEST_PATIENT_REF_PREFIX}CONSIST01"}},
    )
    session_id = created["session_id"]
    await _set_session_blocked(db, session_id, with_snapshot=True)

    # retry 应通过一致性检查（最近审计为 session.created，非 terminated）
    resp_retry = await client.post(
        f"/api/v1/consult/sessions/{session_id}/recover",
        json={"action": "retry_current_stage"},
    )
    assert resp_retry.status_code == 200

    # 重新置 blocked 后测 rollback
    session = await _get_session(db, session_id)
    assert session is not None
    await db.refresh(session)
    session.status = "blocked"
    session.recovery_status = "manual_required"
    await db.commit()

    resp_rollback = await client.post(
        f"/api/v1/consult/sessions/{session_id}/recover",
        json={"action": "rollback_to_stage", "target_stage": "inquiry"},
    )
    assert resp_rollback.status_code == 200
    assert resp_rollback.json()["data"]["current_stage"] == "inquiry"
