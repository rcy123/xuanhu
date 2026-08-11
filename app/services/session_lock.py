"""会话级分布式锁（R8-A）。

锁模型
------
PostgreSQL advisory lock 在一条**专属连接**上持有（贯穿整个 SessionLock 生命周期）
是**唯一权威后端**——进入会话临界区之前必须先取得 PG advisory lock 的权威授予。

Redis 守卫是可选的**快速抢占 / 渐进式放量提示**（fast-contention / roll-out hint），
在 PG 之前被尝试，但**不**是权威后端，也**不能**仅凭自身提供互斥：

- Redis key 有 TTL，到期后第一个写者仍在临界区内，第二个写者会误以为锁已释放；
- Redis 部分可达时，一个进程用 Redis、另一个回退到 PG，两把锁可能同时成立。

只要 PG advisory lock 是唯一权威后端，任意两个**当前版本**（R8-A）进程都不会在
同一会话临界区同时进入。Redis 守卫 key 即便过期或被清，也只是失去一个"提示"，
不会放宽互斥。

滚动升级兼容桥（rolling-deploy compatibility bridge）
-------------------------------------------------------
旧版本（pre-R8-A）进程以**原始 session_id** 为 Redis key
（``xuanhu:session_lock:{raw}``），并且 Redis 可用时**只**取 Redis 锁、不再触碰
PG；新版本以 SHA-256 摘要为 key 且以 PG 为权威。若新旧进程在健康 Redis 下各持
一把 key，就会在临界区产生 split-brain——因此**仅靠 Redis 作"放量守卫"而无桥接，
互斥并不成立**。

为此，当 ``session_id`` 是**规范 UUID**（canonical lowercase UUID）时，本实现会
**同时**取得两把 Redis 守卫：

1. **legacy 原始 key**（与旧进程共享，构成互斥桥）；
2. **hashed 摘要 key**（新进程间的权威提示）。

获取顺序为 **legacy-first → hashed**；若第二把冲突，用 compare-delete 回滚第一把
并抛 ``SessionBusyError``。这样在健康 Redis 下，无论旧进程先到还是新进程先到，
双方都会在 legacy 原始 key 上互斥，不会同时进入临界区（见 ``_legacy_redis_key``
与 ``tests/test_session_lock_unit.py`` 中的桥接测试）。两把 key 均为单 key 命令
（SET NX / 单 key compare-delete Lua），无跨 slot 多 key 脚本。

隐私与退役权衡（privacy / retirement trade-off）
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
legacy 原始 key 会嵌入 session_id 原文，因此**只允许**规范 UUID 进入 Redis key；
非规范 / 任意文本的 session_id 一律 **hashed-only**（绝不把未校验的路径 / 临床文本
写入 Redis key 或日志）。规范 UUID 经过 ``uuid.UUID`` 严格校验，不含临床自由文本。
一旦滚动升级完成、旧进程全部下线，legacy key 即可随 R8-A 迁移退役。

非 UUID 会话的混合版本局限（documented limitation）
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
对**非规范 UUID** 的 session_id，新进程不写 legacy key（隐私优先），因此无法与仍
持有原始非 UUID key 的旧进程互斥。此类会话在混合版本、Redis 健康的窗口期内存在
不可避免的兼容性缺口；新进程之间仍由 hashed key + PG 权威保证互斥。实际部署中
会话 id 均为规范 UUID，故该缺口在常规滚动升级中不出现。

连接与生命周期
--------------
- 专属连接来自 ``get_session_factory()``（与业务 AsyncSession 独立），
  在 ``acquire()`` 中打开并持有，``release()`` 中解锁后归还。
- 不依赖业务 AsyncSession 在 commit 后仍保留物理连接：专属连接自持，
  业务会话的内部分区提交不会丢锁。
- acquire/release 在**每个异常与取消点**都做清理；release 幂等、可重复释放，
  且由独立共享清理任务保证：一旦开始释放，全部资源（两把 Redis 守卫、PG 锁、
  专属连接）只被释放/关闭一次，调用方自身的取消仅在清理完成后才被重新抛出。

隐私
----
- Redis hashed key 基于 session_id 的定长摘要构造（固定隐私安全摘要 namespace）。
- 唯一例外是上面的 legacy 兼容桥：仅对**规范 UUID** 才把 UUID 原文拼入 key；
  任意未校验文本一律 hashed-only。
- 用随机不透明 owner token 作为 Redis 值，绝不使用/记录/存储原始 trace_id 或
  会话临床数据。
- 日志与错误 detail 只用 session_id 的定长摘要（``_bounded_ref``），不回显任意
  未校验的路径原文。
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import logging
import secrets
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager

from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.exceptions import SessionBusyError
from app.core.redis import get_redis

logger = logging.getLogger("xuanhu.session_lock")

# Redis 锁 key 前缀
_LOCK_KEY_PREFIX = "xuanhu:session_lock:"

# PG advisory lock ID 上限（int64）
_ADVISORY_LOCK_MAX = 2**63 - 1

# 日志/详情中使用的 session 摘要长度（16 hex chars）
_REF_LENGTH = 16


def _redis_key(session_id: str) -> str:
    """构造 Redis 锁 key。

    基于 session_id 的定长摘要（固定隐私安全摘要 namespace），绝不把原始
    session_id 拼入 key。
    """
    digest = hashlib.sha256(session_id.encode("utf-8")).hexdigest()[:32]
    return f"{_LOCK_KEY_PREFIX}{digest}"


def _is_canonical_uuid(session_id: str) -> bool:
    """仅当 session_id 是**规范小写 UUID** 表示时返回 True。

    严格校验：``uuid.UUID`` 接受无连字符 / 大小写等形式，这里要求
    ``str(uuid.UUID(session_id)) == session_id``，即输入已是最小化连字符、小写的
    规范形。用于决定是否允许把 UUID 原文写入 Redis 兼容 key（隐私边界）。
    """
    try:
        return str(uuid.UUID(session_id)) == session_id
    except (ValueError, AttributeError, TypeError):
        return False


def _legacy_redis_key(session_id: str) -> str | None:
    """滚动升级兼容桥的 legacy 原始 key；非规范 UUID 返回 None。

    旧版本（pre-R8-A）进程以原始 session_id 为 Redis key。为在健康 Redis 下与旧进程
    互斥，本实现对**规范 UUID** 会话额外取得该 legacy key（legacy-first 顺序）。
    仅当 ``_is_canonical_uuid`` 成立时才返回——避免把任意未校验的路径 / 临床文本拼入
    Redis key。非 UUID 输入保持 hashed-only，返回 None。

    隐私/退役权衡：该 key 嵌入 UUID 原文，仅限规范 UUID；滚动升级完成后即可随 R8-A
    迁移退役。
    """
    if not _is_canonical_uuid(session_id):
        return None
    return f"{_LOCK_KEY_PREFIX}{session_id}"


def _advisory_lock_id(session_id: str) -> int:
    """从 session_id 生成 PG advisory lock ID（bigint）。"""
    digest = hashlib.sha256(session_id.encode()).digest()
    # 取前 8 字节转为有符号 int64
    lock_id = int.from_bytes(digest[:8], byteorder="big", signed=True)
    # 确保为正数（pg_advisory_lock 需要 bigint 范围）
    return lock_id & _ADVISORY_LOCK_MAX


def _bounded_ref(session_id: str) -> str:
    """返回 session_id 的定长摘要，用于日志与错误 detail（不回显原文）。"""
    return hashlib.sha256(session_id.encode("utf-8")).hexdigest()[:_REF_LENGTH]


def _err_type(exc: BaseException) -> str:
    """返回异常类名（有界，不携带任意异常文本）。"""
    return type(exc).__name__


# Redis 值校验 + 删除的原子脚本（校验 owner token 后再 DEL）。
# 用于：① 常规释放；② PG 获取失败时回滚已抢占的 Redis 守卫。
_LUA_RELEASE = """
if redis.call("GET", KEYS[1]) == ARGV[1] then
    return redis.call("DEL", KEYS[1])
else
    return 0
end
"""


class SessionLock:
    """会话写操作分布式锁（R8-A：PG 权威 + Redis 可选守卫）。

    用法（上下文管理器）：

        lock = SessionLock(db, session_id, trace_id)
        async with lock:
            # 写操作

    获取失败时抛出 SessionBusyError (409 SESSION_BUSY)。

    ``db`` 仅用于保持调用方 API 兼容，不再作为锁连接来源；锁使用独立专属连接。
    """

    def __init__(
        self,
        db: AsyncSession,
        session_id: str,
        trace_id: str,
        *,
        _redis_getter: Callable[[], Awaitable[Redis | None]] | None = None,
        _lock_session_factory: Callable[[], AsyncSession] | None = None,
    ) -> None:
        del trace_id  # 隐私：不存储/不记录原始 trace_id
        self._db = db
        self._session_id = session_id
        self._safe_ref = _bounded_ref(session_id)
        self._lock_id = _advisory_lock_id(session_id)
        # 随机不透明 owner token（绝不用 trace_id 或临床数据作为 Redis 值）
        self._owner_token = secrets.token_hex(16)

        self._held = False
        self._lock_type: str | None = None  # 兼容旧字段：权威后端恒为 "pg_advisory"
        self._redis: Redis | None = None
        # 兼容桥可同时持有多把 Redis 守卫（legacy 原始 key + hashed 摘要 key）。
        self._redis_guard_keys: list[str] = []
        self._pg_session: AsyncSession | None = None
        # 共享清理任务：首次 release 创建，并发的重复 release 复用；取消安全。
        self._cleanup_task: asyncio.Task[None] | None = None

        self._redis_getter = _redis_getter or get_redis
        if _lock_session_factory is not None:
            self._lock_session_factory = _lock_session_factory
        else:
            self._lock_session_factory = _default_lock_session_factory

    @property
    def _redis_guard_held(self) -> bool:
        """当前是否持有任一 Redis 守卫（兼容桥可同时持 legacy + hashed 两把）。"""
        return bool(self._redis_guard_keys)

    async def __aenter__(self) -> SessionLock:
        await self.acquire()
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.release()

    async def acquire(self) -> None:
        """获取会话锁：先尝试可选 Redis 守卫（兼容桥双 key），再获取权威 PG advisory lock。

        幂等：已持有则直接返回。异常/取消路径下回滚已抢占的 Redis 守卫。
        若上一次 release 的清理任务仍在进行，先等待其结束，避免在资源尚未释放时
        重新进入。

        兼容桥（见模块文档）：规范 UUID 会话按 **legacy-first → hashed** 顺序取得两把
        Redis 守卫，使新旧进程在健康 Redis 下仍于 legacy 原始 key 上互斥；第二把冲突
        时用 compare-delete 回滚第一把并抛 ``SessionBusyError``。非 UUID 会话仅取得
        hashed 守卫（隐私优先）。
        """
        if self._cleanup_task is not None:
            # 共享清理可能仍在进行（或已完成）：等待其结束，确保不会在资源尚未
            # 释放时重新进入临界区。用 shield 避免 acquire 被取消时波及清理任务。
            await asyncio.shield(self._cleanup_task)
            self._cleanup_task = None
        if self._held:
            return
        settings = get_settings()
        ttl = settings.session_lock_ttl_seconds

        # 1) 可选 Redis 保守守卫（best-effort，不参与互斥判定；PG 仍为权威）
        redis: Redis | None = None
        try:
            redis = await self._redis_getter()
        except Exception as exc:
            redis = None
            logger.warning(
                "session-lock redis client unavailable; proceeding via PG (ref=%s, error=%s)",
                self._safe_ref,
                _err_type(exc),
            )

        # 记录 Redis 客户端（供守卫获取回滚 / release 时释放守卫使用；须在守卫获取
        # 之前赋值，使 SET 异常/冲突路径的 compare-delete 能借由该客户端执行）。
        self._redis = redis

        held_keys: list[str] = []
        if redis is not None:
            # 待获取守卫（按序）：legacy 原始 key（仅规范 UUID）→ hashed 摘要 key。
            guard_keys: list[str] = []
            legacy_key = _legacy_redis_key(self._session_id)
            if legacy_key is not None:
                guard_keys.append(legacy_key)
            guard_keys.append(_redis_key(self._session_id))
            try:
                for key in guard_keys:
                    got = await redis.set(key, self._owner_token, nx=True, ex=ttl)
                    if got:
                        held_keys.append(key)
                    else:
                        # 任一桥接 key 返回真实 NX 冲突 → 保留 SESSION_BUSY 语义。
                        # 由 except 分支回滚本尝试内已取得的守卫后重抛。
                        raise SessionBusyError(
                            message="会话正在处理其他请求，请稍后重试",
                            detail="SESSION_BUSY: session already locked",
                            retryable=True,
                        )
            except SessionBusyError:
                # 回滚本次已取得的守卫（compare-delete，错属不删），不误删他人 key。
                await self._rollback_guards(held_keys)
                raise
            except Exception as exc:
                # Redis SET 出错 → 安全降级到 PG（不绕过、不漏锁）。对**当前**失败的
                # key 也做一次幂等 compare-delete（SET 超时/错误可能在服务端已生效，
                # 避免残留"幽灵守卫"直到 TTL），并回滚已取得的守卫；清理失败也无所谓
                # （安全优先）。
                with contextlib.suppress(Exception):
                    await self._compare_delete(guard_keys[len(held_keys)], self._owner_token)
                await self._rollback_guards(held_keys)
                held_keys.clear()  # 已降级到 PG，本尝试不再持有任何守卫
                logger.warning(
                    "session-lock redis guard failed; falling back to PG (ref=%s, error=%s)",
                    self._safe_ref,
                    _err_type(exc),
                )

        # 2) 权威 PG advisory lock（专属连接）
        try:
            await self._acquire_pg()
        except BaseException:
            # 任何异常/取消：回滚已抢占的 Redis 守卫，避免残留"提示"key。
            if held_keys:
                with contextlib.suppress(BaseException):
                    await self._rollback_guards(held_keys)
            raise

        self._redis_guard_keys = held_keys
        self._held = True

    async def _acquire_pg(self) -> None:
        """在专属连接上获取 PG advisory lock（权威后端）。

        连接冲突 → SessionBusyError；连接/基础设施错误 → 原样抛出（fail-closed，
        不会在未持锁情况下进入临界区）。
        """
        session = self._lock_session_factory()
        try:
            result = await session.execute(text("SELECT pg_try_advisory_lock(:id)"), {"id": self._lock_id})
        except BaseException:
            # 获取阶段异常/取消：确保新建的连接被关闭（不被取消/异常打断）。
            with contextlib.suppress(BaseException):
                await session.close()
            raise
        acquired = bool(result.scalar())
        if not acquired:
            await session.close()
            raise SessionBusyError(
                message="会话正在处理其他请求，请稍后重试",
                detail="SESSION_BUSY: session advisory lock conflict",
                retryable=True,
            )
        self._pg_session = session
        self._lock_type = "pg_advisory"

    async def release(self) -> None:
        """释放锁；幂等、可重复释放、异常/取消安全。

        首次调用启动**共享清理任务**（``_run_cleanup``）。该任务独立于调用方，
        不受调用方取消影响，保证全部资源（Redis 守卫、PG advisory lock、专属连接）
        只被释放/关闭**一次**。并发/重复调用 await 同一个清理任务，确保所有调用方
        都在资源真正释放之后才返回。调用方自身的取消仅在清理完成后才被重新抛出。
        """
        task = self._cleanup_task
        if task is None:
            self._held = False
            task = asyncio.ensure_future(self._run_cleanup())
            self._cleanup_task = task
        try:
            # 用 shield 等待：调用方取消只会取消盾的 outer future，不会把取消
            # 传递到共享清理任务本身，确保清理始终运行到完成。
            await asyncio.shield(task)
        except asyncio.CancelledError:
            # 清理仍在共享任务中继续；等待其完成后再重抛取消，绝不在资源尚未
            # 释放时返回。
            with contextlib.suppress(BaseException):
                await asyncio.shield(task)
            raise

    async def _run_cleanup(self) -> None:
        """一次性、幂等的资源清理（运行于独立共享任务，不受调用方取消影响）。

        依次释放全部 Redis 守卫（每把 compare-delete）→ 释放 PG advisory lock →
        关闭专属连接。每阶段异常都以有界日志记录并继续下一阶段，绝不向外抛出、
        不掩盖业务异常；关闭专属连接是最终兜底，即使解锁失败也会执行。
        """
        keys = self._redis_guard_keys
        self._redis_guard_keys = []
        for key in keys:
            try:
                await self._release_redis_guard(key)
            except BaseException as exc:
                # Redis 守卫释放失败：best-effort，不阻塞 PG 权威锁释放。
                logger.warning(
                    "session-lock redis guard release failed (ref=%s, error=%s)",
                    self._safe_ref,
                    _err_type(exc),
                )

        session = self._pg_session
        self._pg_session = None
        if session is not None:
            try:
                await session.execute(text("SELECT pg_advisory_unlock(:id)"), {"id": self._lock_id})
            except BaseException as exc:
                logger.warning(
                    "session-lock pg unlock failed (ref=%s, error=%s)",
                    self._safe_ref,
                    _err_type(exc),
                )
            finally:
                try:
                    await session.close()
                except BaseException as exc:
                    logger.warning(
                        "session-lock connection close failed (ref=%s, error=%s)",
                        self._safe_ref,
                        _err_type(exc),
                    )
        self._lock_type = None
        self._held = False

    async def _release_redis_guard(self, key: str) -> None:
        """释放单个 Redis 守卫（校验 owner token 后原子删除；错属时不删）。"""
        await self._compare_delete(key, self._owner_token)

    async def _compare_delete(self, key: str, owner: str) -> None:
        """对单把 Redis 守卫做 owner-token 校验后的原子删除（错属不删）。"""
        if self._redis is None:
            return
        # redis.asyncio eval 返回 Awaitable[str] | str 的重载无法精确对齐 awaits；
        # 结果被丢弃，仅需保证协程执行，故定向忽略（与既有 redis.eval 处理一致）。
        await self._redis.eval(_LUA_RELEASE, 1, key, owner)  # type: ignore[misc]

    async def _rollback_guards(self, keys: list[str]) -> None:
        """回滚一批已取得的 Redis 守卫（幂等 compare-delete；清理失败安全忽略）。"""
        for key in keys:
            with contextlib.suppress(BaseException):
                await self._compare_delete(key, self._owner_token)


def _default_lock_session_factory() -> AsyncSession:
    """默认专属连接：来自全局 session factory（与业务会话独立）。"""
    from app.db.session import get_session_factory

    return get_session_factory()()


@asynccontextmanager
async def session_lock(
    db: AsyncSession,
    session_id: str,
    trace_id: str,
) -> AsyncIterator[None]:
    """会话锁上下文管理器便捷函数。

    用法：

        async with session_lock(db, session_id, trace_id):
            # 写操作
    """
    lock = SessionLock(db, session_id, trace_id)
    await lock.acquire()
    try:
        yield
    finally:
        await lock.release()
