"""R8-A 会话锁确定性单元测试（使用 fakes，不依赖真实 PG/Redis）。

覆盖：
- Redis 客户端 ping 后故障 / SET 异常 → 安全降级到 PG，不绕过、不漏锁
- Redis 真实 NX 冲突 → 保留 SESSION_BUSY 语义
- PG 获取失败 → 回滚已抢占的 Redis 守卫
- 错属（wrong-owner）释放 → 不删除他人 Redis 守卫
- acquire/release 期间的取消 → 连接关闭、守卫回滚、状态幂等
- release 的共享清理任务：Redis compare-delete / PG unlock 阻塞期间的真实取消
  时序 → 清理完成、连接关闭、后续持有者可获取
- 并发双重释放共享同一清理；Redis SET 异常后的幽灵守卫 compare-delete 清理
- Redis 锁 key 使用定长摘要 namespace，绝不嵌入原始 session_id
- 幂等清理：重复释放 / 未获取即释放 / 重复获取均安全
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import Any, cast

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import SessionBusyError
from app.services.session_lock import SessionLock

pytestmark = [pytest.mark.asyncio(loop_scope="function")]


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _FakeScalar:
    def __init__(self, value: Any) -> None:
        self._value = value

    def scalar(self) -> Any:
        return self._value


class _FakePgSession:
    """模拟专属 PG 连接 session。

    ``execute`` 依据语句区分 advisory_lock（取锁）与 advisory_unlock（释放）。
    """

    def __init__(
        self,
        *,
        acquire_result: bool = True,
        fail_acquire: BaseException | None = None,
        fail_unlock: BaseException | None = None,
    ) -> None:
        self.acquire_result = acquire_result
        self.fail_acquire = fail_acquire
        self.fail_unlock = fail_unlock
        self.closed = False
        self.executed: list[str] = []

    async def execute(self, stmt: Any, params: Any | None = None) -> _FakeScalar:
        s = str(stmt)
        self.executed.append(s)
        if "advisory_unlock" in s:
            if self.fail_unlock is not None:
                raise self.fail_unlock
            return _FakeScalar(True)
        if self.fail_acquire is not None:
            raise self.fail_acquire
        return _FakeScalar(self.acquire_result)

    async def close(self) -> None:
        self.closed = True


class _FakePgFactory:
    """返回独立 fake session 并记录全部创建结果（供断言连接清理）。"""

    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs
        self.sessions: list[_FakePgSession] = []

    def __call__(self) -> _FakePgSession:
        session = _FakePgSession(**self.kwargs)
        self.sessions.append(session)
        return session


class _FakeRedisLock:
    def __init__(
        self,
        *,
        set_result: bool = True,
        set_error: BaseException | None = None,
        eval_result: int = 1,
    ) -> None:
        self.set_result = set_result
        self.set_error = set_error
        self.eval_result = eval_result
        self.set_calls: list[tuple[Any, ...]] = []
        self.eval_calls: list[tuple[Any, ...]] = []

    async def set(self, key: str, value: str, *, nx: bool = False, ex: int | None = None) -> bool:
        self.set_calls.append((key, value, nx, ex))
        if self.set_error is not None:
            raise self.set_error
        return self.set_result

    async def eval(self, *args: Any, **kwargs: Any) -> int:  # noqa: ARG002
        self.eval_calls.append(args)
        return self.eval_result


class _DictRedis:
    """内存版 Redis 兼容 fake：真实实现 SET NX 与 compare-delete eval。

    用同一份 ``store`` 同时充当新旧进程的共享 key 空间，用于验证滚动升级兼容桥的
    双向互斥与回滚（两把守卫的取得/释放可被精确断言）。
    """

    def __init__(
        self,
        *,
        raise_on_set_key: str | None = None,
    ) -> None:
        self.store: dict[str, str] = {}
        self.set_calls: list[tuple[Any, Any, bool, int | None]] = []
        self.eval_calls: list[tuple[str, str]] = []
        self.raise_on_set_key = raise_on_set_key

    async def set(self, key: str, value: str, *, nx: bool = False, ex: int | None = None) -> bool:
        self.set_calls.append((key, value, nx, ex))
        if self.raise_on_set_key is not None and key == self.raise_on_set_key:
            raise RuntimeError("set boom")
        if nx and key in self.store:
            return False
        self.store[key] = value
        return True

    async def eval(self, script: str, numkeys: int, key: str, value: str) -> int:  # noqa: ARG002
        del script, numkeys
        self.eval_calls.append((key, value))
        if self.store.get(key) == value:
            del self.store[key]
            return 1
        return 0


class _BlockingRedisGuard:
    """Redis 守卫 fake：eval（compare-delete）可被事件阻塞，用于真实取消时序测试。"""

    def __init__(self) -> None:
        self.eval_entered = asyncio.Event()
        self.eval_proceed = asyncio.Event()
        self.eval_calls = 0
        self.set_calls = 0

    async def set(self, key: str, value: str, *, nx: bool = False, ex: int | None = None) -> bool:
        del key, value, nx, ex
        self.set_calls += 1
        return True

    async def eval(self, *args: Any, **kwargs: Any) -> int:  # noqa: ARG002
        del args, kwargs
        self.eval_calls += 1
        self.eval_entered.set()
        await self.eval_proceed.wait()
        return 1


class _BlockingPgSession:
    """专属 PG 连接 fake：advisory_unlock 可被事件阻塞，用于真实取消时序测试。"""

    def __init__(self) -> None:
        self.closed = False
        self.executed: list[str] = []
        self.unlock_entered = asyncio.Event()
        self.unlock_proceed = asyncio.Event()

    async def execute(self, stmt: Any, params: Any | None = None) -> _FakeScalar:
        del params
        s = str(stmt)
        self.executed.append(s)
        if "advisory_unlock" in s:
            self.unlock_entered.set()
            await self.unlock_proceed.wait()
            return _FakeScalar(True)
        return _FakeScalar(True)

    async def close(self) -> None:
        self.closed = True


class _BlockingPgFactory:
    """返回可阻塞的专属 PG fake 会话。"""

    def __init__(self) -> None:
        self.sessions: list[_BlockingPgSession] = []

    def __call__(self) -> _BlockingPgSession:
        session = _BlockingPgSession()
        self.sessions.append(session)
        return session


def _redis_getter(client: _FakeRedisLock | None, *, raise_getter: bool = False):
    async def _get():
        if raise_getter:
            raise RuntimeError("redis down")
        return client

    return _get


class _DummyDB:
    pass


def _dummy_db() -> AsyncSession:
    return cast(AsyncSession, _DummyDB())


# ---------------------------------------------------------------------------
# 辅助构造
# ---------------------------------------------------------------------------


def _make(
    *,
    redis: _FakeRedisLock | None = None,
    raise_getter: bool = False,
    pg_factory: _FakePgFactory | None = None,
    session_id: str = "550e8400-e29b-41d4-a716-446655440000",
) -> tuple[SessionLock, _FakePgFactory, _FakeRedisLock | None]:
    pg = pg_factory or _FakePgFactory()
    lock = SessionLock(
        _dummy_db(),
        session_id,
        "trace-secret",
        _redis_getter=_redis_getter(redis, raise_getter=raise_getter),
        _lock_session_factory=pg,
    )
    return lock, pg, redis


# ---------------------------------------------------------------------------
# Redis 故障降级
# ---------------------------------------------------------------------------


async def test_redis_getter_outage_falls_back_to_pg() -> None:
    """get_redis 故障（ping 后客户端不可用）→ 仅 PG，Redis 守卫未持。"""
    lock, pg, _ = _make(redis=None, raise_getter=True)
    await lock.acquire()
    assert lock._held is True
    assert lock._redis_guard_held is False
    assert len(pg.sessions) == 1
    assert pg.sessions[0].closed is False  # 持有中
    await lock.release()
    assert pg.sessions[0].closed is True


async def test_redis_set_exception_after_client_degrades_safely() -> None:
    """Redis 客户端已创建但 SET 抛异常 → 降级到 PG，不 bypass、不漏锁。

    同时：SET 出错时做一次 best-effort compare-delete，避免残留"幽灵守卫"。
    """
    redis = _FakeRedisLock(set_error=RuntimeError("set boom"))
    lock, pg, redis_client = _make(redis=redis)
    await lock.acquire()
    assert redis_client is not None
    assert len(redis_client.set_calls) == 1
    # SET 异常后必须用 owner token 做一次 compare-delete（幂等清理幽灵守卫）
    assert len(redis_client.eval_calls) == 1
    assert lock._redis_guard_held is False  # SET 失败视为未持守卫
    assert lock._held is True
    assert len(pg.sessions) == 1
    await lock.release()


async def test_redis_key_does_not_embed_raw_session_id() -> None:
    """Redis 锁 key 使用定长摘要 namespace，绝不包含原始 session_id 文本。"""
    from app.services.session_lock import _redis_key

    raw = "<script>alert(1)</script>/unsafe path/患者主诉原文"
    key = _redis_key(raw)
    assert raw not in key
    # 同一 session 稳定；不同 session 大概率不同
    assert _redis_key(raw) == _redis_key(raw)
    assert _redis_key(raw) != _redis_key("另一个未校验的原始文本")


async def test_redis_nx_conflict_preserves_session_busy() -> None:
    """Redis 真实 NX 冲突 → SESSION_BUSY，且不触碰 PG。"""
    redis = _FakeRedisLock(set_result=False)
    lock, pg, _ = _make(redis=redis)
    with pytest.raises(SessionBusyError) as exc_info:
        await lock.acquire()
    assert exc_info.value.code == "SESSION_BUSY"
    assert exc_info.value.retryable is True
    assert len(pg.sessions) == 0  # 冲突发生在取 PG 之前
    assert lock._held is False


async def test_pg_conflict_releases_redis_guard() -> None:
    """Redis 守卫已持 + PG 冲突 → SESSION_BUSY，并回滚 Redis 守卫。"""
    redis = _FakeRedisLock(set_result=True)
    pg = _FakePgFactory(acquire_result=False)
    lock, pg_factory, redis_client = _make(redis=redis, pg_factory=pg)
    with pytest.raises(SessionBusyError) as exc_info:
        await lock.acquire()
    assert exc_info.value.code == "SESSION_BUSY"
    # PG 冲突后必须删除已抢占的 Redis 守卫（eval 为 compare-and-delete）
    assert redis_client is not None
    assert len(redis_client.eval_calls) >= 1
    assert lock._held is False


# ---------------------------------------------------------------------------
# 错属释放
# ---------------------------------------------------------------------------


async def test_wrong_owner_release_does_not_delete_others_guard() -> None:
    """Redis 守卫已被他人接管（eval 返回 0）→ 释放不误删、且 PG 正常释放。

    使用非 UUID 会话 → 仅持有 hashed 单把守卫，使 compare-delete 次数断言（恰一次）
    不受兼容桥双 key 影响而保持清晰。
    """
    redis = _FakeRedisLock(set_result=True, eval_result=0)  # 0 = 值不符，不删除
    lock, pg, redis_client = _make(redis=redis, session_id="wrong-owner-non-uuid")
    await lock.acquire()
    assert lock._redis_guard_held is True
    await lock.release()
    # PG 释放成功（unlock 语句执行 + 连接关闭）
    assert len(pg.sessions) == 1
    assert any("advisory_unlock" in s for s in pg.sessions[0].executed)
    assert pg.sessions[0].closed is True
    # Redis 侧：evaluation 执行过（compare-and-delete），返回 0 表示未误删
    assert redis_client is not None
    assert len(redis_client.eval_calls) == 1


# ---------------------------------------------------------------------------
# 取消安全
# ---------------------------------------------------------------------------


async def test_cancellation_during_acquire_cleans_up() -> None:
    """acquire 期间取消 → 关闭 PG 连接并回滚 Redis 守卫。"""
    redis = _FakeRedisLock(set_result=True)
    pg = _FakePgFactory(fail_acquire=asyncio.CancelledError())
    lock, pg_factory, redis_client = _make(redis=redis, pg_factory=pg)
    with pytest.raises(asyncio.CancelledError):
        await lock.acquire()
    assert len(pg_factory.sessions) == 1
    assert pg_factory.sessions[0].closed is True  # 连接已关闭
    assert redis_client is not None
    assert len(redis_client.eval_calls) >= 1  # 守卫已回滚
    assert lock._held is False


async def test_release_unlock_error_swallowed_connection_closed() -> None:
    """释放阶段 PG 解锁抛非取消异常 → 吞掉、连接仍关闭、不抛出、状态幂等。"""
    redis = _FakeRedisLock(set_result=True)
    pg = _FakePgFactory(fail_unlock=RuntimeError("unlock boom"))
    lock, pg_factory, _ = _make(redis=redis, pg_factory=pg)
    await lock.acquire()
    assert len(pg_factory.sessions) == 1
    session = pg_factory.sessions[0]
    await lock.release()  # 解锁失败不向外抛（不掩盖业务异常）
    assert session.closed is True
    assert lock._held is False
    assert lock._pg_session is None
    assert lock._redis_guard_held is False
    # 解锁失败后的再次释放 → no-op（幂等）
    await lock.release()


async def test_cancel_while_redis_guard_release_blocked() -> None:
    """取消发生在 Redis compare-delete 阻塞期间 → 清理完成、连接关闭、状态清空。

    用阻塞事件驱动真实取消时序（而非注入 execute 的 CancelledError）。
    """
    redis = _BlockingRedisGuard()
    pg = _FakePgFactory()
    # 非 UUID 会话 → 仅持 hashed 单把守卫，使 compare-delete 阻塞期间的计数断言
    # （eval_calls == 1）不受兼容桥双 key 影响而保持清晰。
    lock, pg_factory, redis_client = _make(  # type: ignore[arg-type]
        redis=redis, pg_factory=pg, session_id="cancel-blocked-non-uuid"
    )
    await lock.acquire()
    assert lock._redis_guard_held is True

    release_task = asyncio.create_task(lock.release())
    await redis.eval_entered.wait()  # 清理已阻塞在 Redis compare-delete
    release_task.cancel()
    redis.eval_proceed.set()  # 放行 Redis 清理，让共享清理任务继续
    with pytest.raises(asyncio.CancelledError):
        await release_task

    # Redis 与 PG 清理都已完成，连接关闭，状态清空
    assert redis_client is not None
    assert redis_client.eval_calls == 1
    assert lock._redis_guard_held is False
    assert len(pg_factory.sessions) == 1
    assert any("advisory_unlock" in s for s in pg_factory.sessions[0].executed)
    assert pg_factory.sessions[0].closed is True
    assert lock._held is False
    assert lock._pg_session is None
    # 后续持有者可以获取锁
    lock2, pg2, _ = _make()
    await lock2.acquire()
    await lock2.release()


async def test_cancel_while_pg_unlock_blocked() -> None:
    """取消发生在 PG unlock 阻塞期间 → 清理完成、连接关闭、后续持有者可获取。"""
    redis = _FakeRedisLock(set_result=True)
    pg = _BlockingPgFactory()
    lock, pg_factory, _ = _make(redis=redis, pg_factory=pg)  # type: ignore[arg-type]
    await lock.acquire()
    assert len(pg_factory.sessions) == 1
    session = pg_factory.sessions[0]

    release_task = asyncio.create_task(lock.release())
    await session.unlock_entered.wait()  # 清理已阻塞在 PG unlock
    release_task.cancel()
    session.unlock_proceed.set()  # 放行 PG 清理
    with pytest.raises(asyncio.CancelledError):
        await release_task

    assert session.closed is True
    assert lock._held is False
    assert lock._pg_session is None
    assert lock._redis_guard_held is False
    # 后续持有者可以获取锁
    lock2, _, _ = _make()
    await lock2.acquire()
    await lock2.release()


async def test_concurrent_double_release_shares_one_cleanup() -> None:
    """并发双重释放共享同一个清理任务：Redis 守卫只释放一次。

    使用非 UUID 会话 → 仅持 hashed 单把守卫，使 compare-delete 次数断言（恰一次）
    不受兼容桥双 key 影响而保持清晰。
    """
    redis = _FakeRedisLock(set_result=True)
    pg = _FakePgFactory()
    lock, pg_factory, redis_client = _make(redis=redis, pg_factory=pg, session_id="concurrent-release-non-uuid")
    await lock.acquire()
    assert redis_client is not None

    release_a = asyncio.create_task(lock.release())
    release_b = asyncio.create_task(lock.release())
    await asyncio.gather(release_a, release_b)

    # 共享同一清理：Redis eval 恰好一次，PG 连接关闭一次
    assert len(redis_client.eval_calls) == 1
    assert len(pg_factory.sessions) == 1
    assert pg_factory.sessions[0].closed is True
    assert lock._held is False
    assert lock._pg_session is None


# ---------------------------------------------------------------------------
# 幂等清理
# ---------------------------------------------------------------------------


async def test_double_release_is_safe() -> None:
    lock, pg, _ = _make()
    await lock.acquire()
    await lock.release()
    await lock.release()  # 第二次 no-op
    assert len(pg.sessions) == 1


async def test_release_without_acquire_is_safe() -> None:
    lock, pg, _ = _make()
    with contextlib.suppress(Exception):
        await lock.release()
    assert len(pg.sessions) == 0


async def test_double_acquire_is_idempotent() -> None:
    lock, pg, _ = _make()
    await lock.acquire()
    await lock.acquire()  # 幂等，不重复开连接
    assert len(pg.sessions) == 1
    await lock.release()


async def test_different_sessions_get_different_lock_ids() -> None:
    """不同 session_id 使用不同 advisory lock id（互不冲突）。"""
    sid_a = "550e8400-e29b-41d4-a716-446655440000"
    sid_b = "550e8400-e29b-41d4-a716-446655440001"
    from app.services.session_lock import _advisory_lock_id

    assert _advisory_lock_id(sid_a) != _advisory_lock_id(sid_b)


# ---------------------------------------------------------------------------
# 滚动升级兼容桥（rolling-deploy compatibility bridge）
# ---------------------------------------------------------------------------

_CANON_UUID = "550e8400-e29b-41d4-a716-446655440000"


def _legacy_key(sid: str) -> str:
    return f"xuanhu:session_lock:{sid}"


async def test_is_canonical_uuid_strict() -> None:
    """只有规范小写 UUID 才被识别为可写 legacy 原始 key。"""
    from app.services.session_lock import _is_canonical_uuid

    assert _is_canonical_uuid(_CANON_UUID) is True
    # 大小写 / 无连字符等非规范形一律拒绝
    assert _is_canonical_uuid(_CANON_UUID.upper()) is False
    assert _is_canonical_uuid(_CANON_UUID.replace("-", "")) is False
    assert _is_canonical_uuid("test-lock-not-a-uuid") is False
    assert _is_canonical_uuid("<script>/unsafe path/患者主诉原文") is False


async def test_legacy_redis_key_only_for_canonical_uuid() -> None:
    """legacy 原始 key 仅对规范 UUID 返回；非 UUID 返回 None（hashed-only）。"""
    from app.services.session_lock import _legacy_redis_key

    assert _legacy_redis_key(_CANON_UUID) == _legacy_key(_CANON_UUID)
    assert _legacy_redis_key("test-lock-not-a-uuid") is None
    assert _legacy_redis_key("<script>/unsafe path") is None


async def test_bridge_old_first_new_second_excludes() -> None:
    """旧进程先持 legacy 原始 key → 新进程 SESSION_BUSY，且不触碰 hashed/PG。"""
    from app.services.session_lock import _redis_key

    redis = _DictRedis()
    # 模拟旧版本（pre-R8-A）进程已用原始 session_id 抢占 Redis 锁。
    old_owner = "old-owner-token"
    assert await redis.set(_legacy_key(_CANON_UUID), old_owner, nx=True)

    lock, pg, _ = _make(redis=redis, session_id=_CANON_UUID)  # type: ignore[arg-type]
    with pytest.raises(SessionBusyError) as exc_info:
        await lock.acquire()
    assert exc_info.value.code == "SESSION_BUSY"
    # legacy-first 冲突：hashed key 未被取得，PG 也未被触碰。
    assert _redis_key(_CANON_UUID) not in redis.store
    assert len(pg.sessions) == 0
    # 旧进程的 legacy key 必须保持原样（compare-delete 未误删）。
    assert redis.store[_legacy_key(_CANON_UUID)] == old_owner
    assert lock._held is False


async def test_bridge_new_first_old_second_excludes() -> None:
    """新进程先持 legacy+hashed → 旧进程（仅 raw key）取锁被拒。"""
    redis = _DictRedis()
    lock, pg, _ = _make(redis=redis, session_id=_CANON_UUID)  # type: ignore[arg-type]
    await lock.acquire()
    assert lock._redis_guard_held is True
    # 新进程已持有两把守卫。
    assert _legacy_key(_CANON_UUID) in redis.store
    from app.services.session_lock import _redis_key

    assert _redis_key(_CANON_UUID) in redis.store

    # 模拟旧进程：它只 SET NX 原始 session key → 因新进程已占而返回 False（互斥）。
    old_acquired = await redis.set(_legacy_key(_CANON_UUID), "old-owner", nx=True)
    assert old_acquired is False
    await lock.release()
    # 释放后旧进程可重新取得 legacy key（桥已让路）。
    old_acquired = await redis.set(_legacy_key(_CANON_UUID), "old-owner", nx=True)
    assert old_acquired is True


async def test_bridge_partial_second_key_conflict_rolls_back() -> None:
    """legacy 先取得、hashed 冲突 → SESSION_BUSY，且 legacy 被 compare-delete 回滚。"""
    from app.services.session_lock import _redis_key

    redis = _DictRedis()
    hashed_key = _redis_key(_CANON_UUID)
    # 另一新进程已持 hashed key。
    assert await redis.set(hashed_key, "other-owner", nx=True)

    lock, pg, _ = _make(redis=redis, session_id=_CANON_UUID)  # type: ignore[arg-type]
    with pytest.raises(SessionBusyError):
        await lock.acquire()
    # 先取得的 legacy key 必须被回滚（compare-delete），不残留"幽灵守卫"。
    assert _legacy_key(_CANON_UUID) not in redis.store
    # 他人 hashed key 不被误删。
    assert redis.store[hashed_key] == "other-owner"
    assert len(pg.sessions) == 0
    # legacy key 已释放，可供后续持有者取得。
    assert await redis.set(_legacy_key(_CANON_UUID), "later-owner", nx=True) is True


async def test_bridge_set_exception_on_second_key_rolls_back_and_degrades() -> None:
    """hashed（第二把）SET 异常 → 回滚已取得的 legacy 并安全降级到 PG。"""
    from app.services.session_lock import _redis_key

    redis = _DictRedis(raise_on_set_key=_redis_key(_CANON_UUID))
    lock, pg, _ = _make(redis=redis, session_id=_CANON_UUID)  # type: ignore[arg-type]
    await lock.acquire()
    # 先取得的 legacy key 已回滚；SET 异常不持任何守卫，但 PG 权威锁照常持有。
    assert _legacy_key(_CANON_UUID) not in redis.store
    assert lock._redis_guard_held is False
    assert lock._held is True
    assert len(pg.sessions) == 1
    await lock.release()
    assert lock._held is False


async def test_bridge_release_clears_both_keys() -> None:
    """release 对规范 UUID 会话同时释放 legacy + hashed 两把守卫。"""
    from app.services.session_lock import _redis_key

    redis = _DictRedis()
    lock, pg, _ = _make(redis=redis, session_id=_CANON_UUID)  # type: ignore[arg-type]
    await lock.acquire()
    assert redis.store[_legacy_key(_CANON_UUID)] == lock._owner_token
    assert redis.store[_redis_key(_CANON_UUID)] == lock._owner_token
    await lock.release()
    assert _legacy_key(_CANON_UUID) not in redis.store
    assert _redis_key(_CANON_UUID) not in redis.store
    assert lock._redis_guard_held is False


async def test_non_uuid_session_uses_hashed_only_and_never_leaks_raw() -> None:
    """非 UUID 会话仅取 hashed key；原始文本绝不进入任何 Redis key。"""
    from app.services.session_lock import _redis_key

    raw = "<script>alert(1)</script>/unsafe path/患者主诉原文 non-uuid"
    redis = _DictRedis()
    lock, pg, _ = _make(redis=redis, session_id=raw)  # type: ignore[arg-type]
    await lock.acquire()
    assert set(redis.store.keys()) == {_redis_key(raw)}
    assert all(raw not in key for key in redis.store)
    assert lock._redis_guard_held is True
    await lock.release()
    assert redis.store == {}
