"""P3-2 会话锁单元测试。

覆盖：
- PG advisory lock / Redis lock 获取与释放
- 锁冲突（通过独立数据库连接验证）
- SessionLock 上下文管理器正常路径
- 锁释放异常路径
- Redis 可用时优先使用 Redis

本测试为集成测试，需要 PostgreSQL；不可用时自动跳过。
"""

from __future__ import annotations

import contextlib
import uuid

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import SessionBusyError
from app.core.redis import reset_redis
from app.services.session_lock import SessionLock, _advisory_lock_id

pytestmark = [pytest.mark.integration, pytest.mark.asyncio(loop_scope="module")]


@pytest_asyncio.fixture(loop_scope="module")
async def db() -> AsyncSession:
    """提供独立数据库会话。"""
    from app.db.session import get_session_factory

    factory = get_session_factory()
    async with factory() as session:
        yield session


@pytest_asyncio.fixture(loop_scope="module", autouse=True)
async def _check_postgres() -> None:
    """检查 PostgreSQL 可用性。"""
    from app.core.config import get_settings
    from app.db.session import get_session_factory, reset_session_factory

    get_settings.cache_clear()
    await reset_session_factory()
    factory = get_session_factory()
    try:
        async with factory() as session:
            await session.execute(text("SELECT 1"))
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"PostgreSQL 不可用，跳过集成测试: {type(exc).__name__}: {exc}")


@pytest_asyncio.fixture(loop_scope="module", autouse=True)
async def _reset_redis_after_module() -> None:
    """模块结束后清理 Redis 连接。"""
    yield
    with contextlib.suppress(RuntimeError):
        await reset_redis()


# ---------------------------------------------------------------------------
# 辅助函数
# ---------------------------------------------------------------------------


def _new_session_factory():
    """创建新的独立数据库会话（用于跨连接锁测试）。"""
    from app.db.session import get_session_factory

    return get_session_factory()


# ---------------------------------------------------------------------------
# 测试
# ---------------------------------------------------------------------------


async def test_advisory_lock_id_deterministic() -> None:
    """同一 session_id 生成相同的 advisory lock ID。"""
    sid = "550e8400-e29b-41d4-a716-446655440000"
    id1 = _advisory_lock_id(sid)
    id2 = _advisory_lock_id(sid)
    assert id1 == id2
    assert 0 <= id1 < 2**63


async def test_advisory_lock_id_different() -> None:
    """不同 session_id 生成不同的 lock ID（大概率）。"""
    sid1 = "550e8400-e29b-41d4-a716-446655440000"
    sid2 = "550e8400-e29b-41d4-a716-446655440001"
    assert _advisory_lock_id(sid1) != _advisory_lock_id(sid2)


async def test_acquire_release_lock(db: AsyncSession) -> None:
    """锁正常获取与释放（Redis 或 PG advisory lock）。"""
    session_id = f"test-lock-{uuid.uuid4()}"
    trace_id = "trace-acquire-release"

    lock = SessionLock(db, session_id, trace_id)
    await lock.acquire()
    assert lock._lock_type in ("redis", "pg_advisory")

    # 用独立连接验证 PG 锁被持有（仅 PG lock 模式）
    if lock._lock_type == "pg_advisory":
        factory = _new_session_factory()
        async with factory() as session2:
            lock_id = _advisory_lock_id(session_id)
            result = await session2.execute(
                text("SELECT pg_try_advisory_lock(:id)"), {"id": lock_id}
            )
            acquired = result.scalar()
            assert acquired is False, "独立连接应无法获取已被持有的锁"

    await lock.release()

    # 释放后独立连接应可获取 PG 锁
    if lock._lock_type == "pg_advisory":
        factory = _new_session_factory()
        async with factory() as session3:
            lock_id = _advisory_lock_id(session_id)
            result = await session3.execute(
                text("SELECT pg_try_advisory_lock(:id)"), {"id": lock_id}
            )
            acquired = result.scalar()
            assert acquired is True, "释放后独立连接应能获取锁"
            await session3.execute(
                text("SELECT pg_advisory_unlock(:id)"), {"id": lock_id}
            )
            await session3.commit()


async def test_lock_conflict(db: AsyncSession) -> None:
    """同一会话第二次获取锁失败，返回 SESSION_BUSY。"""
    session_id = f"test-lock-conflict-{uuid.uuid4()}"
    trace_id_1 = "trace-first"
    trace_id_2 = "trace-second"

    lock1 = SessionLock(db, session_id, trace_id_1)
    await lock1.acquire()
    lock_type = lock1._lock_type

    if lock_type == "pg_advisory":
        # 用独立连接尝试获取同一锁，应失败
        factory = _new_session_factory()
        async with factory() as session2:
            lock2 = SessionLock(session2, session_id, trace_id_2)
            with pytest.raises(SessionBusyError) as exc_info:
                await lock2.acquire()
            assert exc_info.value.code == "SESSION_BUSY"
            assert exc_info.value.retryable is True
    else:
        # Redis 锁：同一 DB 连接第二次获取也会失败（Redis 不关心连接）
        lock2 = SessionLock(db, session_id, trace_id_2)
        with pytest.raises(SessionBusyError) as exc_info:
            await lock2.acquire()
        assert exc_info.value.code == "SESSION_BUSY"
        assert exc_info.value.retryable is True

    # 释放 lock1
    await lock1.release()

    # 释放后应可重新获取
    lock3 = SessionLock(db, session_id, trace_id_2)
    await lock3.acquire()
    await lock3.release()


async def test_context_manager_normal(db: AsyncSession) -> None:
    """SessionLock 上下文管理器正常路径。"""
    session_id = f"test-lock-ctx-{uuid.uuid4()}"
    trace_id = "trace-ctx"

    async with SessionLock(db, session_id, trace_id) as lock:
        assert lock._lock_type in ("redis", "pg_advisory")

    # 退出上下文后可重新获取（说明锁已释放）
    lock2 = SessionLock(db, session_id, "trace-ctx-2")
    await lock2.acquire()
    await lock2.release()


async def test_context_manager_exception_path(db: AsyncSession) -> None:
    """SessionLock 上下文管理器异常路径仍能释放锁。"""
    session_id = f"test-lock-exc-{uuid.uuid4()}"
    trace_id = "trace-exc"

    class TestError(Exception):
        pass

    with pytest.raises(TestError):
        async with SessionLock(db, session_id, trace_id):
            raise TestError("模拟业务异常")

    # 即使抛异常，锁也应释放，可重新获取
    lock2 = SessionLock(db, session_id, "trace-exc-2")
    await lock2.acquire()
    await lock2.release()


async def test_different_sessions_no_conflict(db: AsyncSession) -> None:
    """不同会话的锁互不冲突。"""
    sid1 = f"test-lock-noconflict-1-{uuid.uuid4()}"
    sid2 = f"test-lock-noconflict-2-{uuid.uuid4()}"

    lock1 = SessionLock(db, sid1, "trace-1")
    lock2 = SessionLock(db, sid2, "trace-2")

    await lock1.acquire()
    await lock2.acquire()  # 不同 session_id，应成功获取

    await lock1.release()
    await lock2.release()


async def test_double_release_safe(db: AsyncSession) -> None:
    """重复释放锁安全（不抛异常）。"""
    session_id = f"test-lock-double-{uuid.uuid4()}"

    lock = SessionLock(db, session_id, "trace-double")
    await lock.acquire()
    await lock.release()
    # 第二次 release 不应抛异常
    await lock.release()


async def test_release_without_acquire_safe(db: AsyncSession) -> None:
    """未获取锁时释放安全（不抛异常）。"""
    session_id = f"test-lock-noacquire-{uuid.uuid4()}"
    lock = SessionLock(db, session_id, "trace-noacquire")
    await lock.release()  # 不应抛异常


async def test_redis_or_pg_lock_succeeds(db: AsyncSession) -> None:
    """锁获取成功（优先 Redis，不可用时降级 PG）。"""
    session_id = f"test-lock-backend-{uuid.uuid4()}"

    lock = SessionLock(db, session_id, "trace-backend")
    await lock.acquire()
    # 无论哪种实现，获取都应成功
    assert lock._lock_type in ("redis", "pg_advisory")
    await lock.release()
