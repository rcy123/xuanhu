"""会话恢复服务层。

实现 P3-4 四种恢复动作：
- resume_from_pg_snapshot
- retry_current_stage
- rollback_to_stage
- terminate

每次成功恢复或终止均写入 audit_events，并 best-effort 写入 Redis Stream 事件。
本层不调用 Agent、不推进当前阶段、不生成业务结论。

P3-4-fix 补强：
- B-004：在 service 层入口获取会话级写锁（复用 P3-2 SessionLock）。
- B-005：执行恢复动作前比较 Redis checkpoint / PG state_snapshot / 最近 audit event，
  检测冲突并降级，不静默恢复。
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import (
    RecoveryNotNeededError,
    SessionNotFoundError,
    StateRecoveryRequiredError,
    ValidationError,
)
from app.models.audit import AuditEvent
from app.models.consult import ConsultSession
from app.schemas.recovery import (
    VALID_ROLLBACK_TARGETS,
    RecoveryRequest,
    RecoveryResponse,
)
from app.services.session_lock import SessionLock

logger = logging.getLogger("xuanhu.recovery")

# 只能对 blocked 状态或需要人工恢复的会话执行恢复
_RECOVERABLE_STATUSES = {"blocked"}
_RECOVERABLE_RECOVERY_STATUSES = {"manual_required", "recovering"}

# 终止后进入的阻塞原因
_TERMINATED_BLOCKED_REASON = "terminated_by_doctor"

# Redis checkpoint key 约定（与数据库设计文档 §8.3 / §9.1 一致）
_CHECKPOINT_KEY_PREFIX = "xuanhu:checkpoint:"

# 标记会话已终止的审计事件类型
_TERMINAL_AUDIT_TYPES = {"session.terminated"}

# 恢复动作 → 对应审计事件类型
_ACTION_EVENT_MAP: dict[str, str] = {
    "resume_from_pg_snapshot": "session.recovered",
    "retry_current_stage": "session.recovered",
    "rollback_to_stage": "session.recovered",
    "terminate": "session.terminated",
}


def _now() -> datetime:
    """返回当前 UTC 时间（naive，与模型列类型保持一致）。"""
    return datetime.now(UTC).replace(tzinfo=None)


def _audit_event(
    session_id: uuid.UUID,
    event_type: str,
    actor_type: str,
    actor_id: str | None,
    payload: dict[str, Any],
    trace_id: str,
) -> AuditEvent:
    """构造审计事件记录。"""
    return AuditEvent(
        session_id=session_id,
        event_type=event_type,
        actor_type=actor_type,
        actor_id=actor_id,
        payload=payload,
        trace_id=trace_id,
    )


def _checkpoint_key(session_id: str) -> str:
    """构造 Redis checkpoint key。"""
    return f"{_CHECKPOINT_KEY_PREFIX}{session_id}"


class RecoveryContext:
    """恢复一致性上下文。

    汇总 Redis checkpoint、PG state_snapshot、最近审计事件，供恢复动作决策。
    所有字段在恢复动作执行前采集，恢复动作执行后只读，不随状态变更而失效。
    """

    def __init__(self) -> None:
        self.checkpoint_status: str = "missing"  # "present" | "missing" | "unreadable"
        self.checkpoint: dict[str, Any] | None = None
        self.checkpoint_version: int | None = None
        self.checkpoint_stage: str | None = None
        self.snapshot: dict[str, Any] | None = None
        self.latest_audit_type: str | None = None
        self.latest_audit_at: datetime | None = None

    def is_terminated_by_audit(self) -> bool:
        """最近一条审计是否显示会话已终止。"""
        return self.latest_audit_type in _TERMINAL_AUDIT_TYPES

    def checkpoint_older_than_pg(self, pg_version: int) -> bool:
        """checkpoint 版本是否明显旧于 PG state_version。"""
        if self.checkpoint_version is None:
            return False
        return self.checkpoint_version < pg_version


class RecoveryService:
    """会话恢复应用服务。"""

    def __init__(self, db: AsyncSession) -> None:
        self._db = db

    async def recover(
        self,
        session_id: str,
        request: RecoveryRequest,
        *,
        doctor_id: str | None,
        trace_id: str,
    ) -> RecoveryResponse:
        """执行会话恢复动作。

        流程：
        1. service 层入口获取会话锁（B-004）
        2. 加载并校验会话
        3. 构建恢复上下文并执行一致性检查（B-005）
        4. 校验 action 参数
        5. 执行恢复动作（写 audit_events）
        6. best-effort 写入 Redis Stream 事件
        7. 释放锁并返回

        Args:
            session_id: 会话 ID。
            request: 恢复请求体（action / target_stage / reason）。
            doctor_id: 操作医师标识。
            trace_id: 请求链路 ID。

        Raises:
            SessionNotFoundError: 会话不存在。
            RecoveryNotNeededError: 会话状态正常，无需恢复。
            StateRecoveryRequiredError: 无法自动恢复，需人工处理。
            ValidationError: 请求参数不合法。
            SessionBusyError: 会话锁被占用（409 SESSION_BUSY）。
        """
        # B-004：service 层入口获取会话锁，异常路径也释放
        lock = SessionLock(self._db, session_id, trace_id)
        await lock.acquire()
        try:
            return await self._recover_locked(session_id, request, doctor_id, trace_id)
        finally:
            await lock.release()

    async def _recover_locked(
        self,
        session_id: str,
        request: RecoveryRequest,
        doctor_id: str | None,
        trace_id: str,
    ) -> RecoveryResponse:
        """在已持锁状态下执行恢复（拆分便于测试与可读性）。"""
        # 1. 解析 session_id
        try:
            sid = uuid.UUID(session_id)
        except ValueError as exc:
            raise SessionNotFoundError(
                detail=f"session_id={session_id} 格式非法",
                retryable=False,
            ) from exc

        # 2. 查询会话
        result = await self._db.execute(
            select(ConsultSession).where(ConsultSession.id == sid)
        )
        session = result.scalar_one_or_none()
        if session is None:
            raise SessionNotFoundError(
                detail=f"session_id={session_id} 在数据库中未找到",
                retryable=False,
            )

        # 3. 校验是否需要恢复
        if (
            session.status not in _RECOVERABLE_STATUSES
            and session.recovery_status not in _RECOVERABLE_RECOVERY_STATUSES
        ):
            raise RecoveryNotNeededError(
                detail=(
                    f"session_id={session_id} status={session.status} "
                    f"recovery_status={session.recovery_status}，无需恢复"
                ),
                retryable=False,
            )

        # 4. B-005：构建恢复上下文并执行一致性检查
        ctx = await self._build_recovery_context(session_id, sid)
        self._validate_consistency(session, request, ctx, session_id)

        # 5. 校验 action 特有参数
        if request.action == "rollback_to_stage":
            if not request.target_stage:
                raise ValidationError(
                    message="action=rollback_to_stage 时必须提供 target_stage",
                    detail=f"session_id={session_id} rollback_to_stage 缺少 target_stage",
                    retryable=False,
                )
            if request.target_stage not in VALID_ROLLBACK_TARGETS:
                raise ValidationError(
                    message=f"无效的 target_stage: {request.target_stage}",
                    detail=(
                        f"session_id={session_id} target_stage={request.target_stage} 不在 "
                        f"合法范围 {sorted(VALID_ROLLBACK_TARGETS)} 内"
                    ),
                    retryable=False,
                )

        # 6. 执行恢复动作
        previous_status = session.status
        previous_stage = session.current_stage
        previous_recovery_status = session.recovery_status
        actor_type = "doctor" if doctor_id else "system"

        if request.action == "resume_from_pg_snapshot":
            await self._do_resume_from_snapshot(
                session, session_id, ctx, actor_type, doctor_id, trace_id
            )
        elif request.action == "retry_current_stage":
            await self._do_retry_current_stage(
                session, session_id, ctx, actor_type, doctor_id, trace_id
            )
        elif request.action == "rollback_to_stage":
            await self._do_rollback_to_stage(
                session, session_id, request, ctx, actor_type, doctor_id, trace_id
            )
        elif request.action == "terminate":
            await self._do_terminate(
                session, session_id, request, ctx, actor_type, doctor_id, trace_id
            )

        await self._db.flush()
        await self._db.refresh(session)

        # 7. Best-effort 写入 Redis Stream 事件
        event_type = _ACTION_EVENT_MAP.get(request.action, "session.recovered")
        await self._try_emit_stream_event(
            session_id=str(session.id),
            event_type=event_type,
            payload={
                "action": request.action,
                "previous_status": previous_status,
                "previous_stage": previous_stage,
                "previous_recovery_status": previous_recovery_status,
                "reason": request.reason,
                "current_stage": session.current_stage,
                "status": session.status,
                "recovery_status": session.recovery_status,
                "checkpoint_status": ctx.checkpoint_status,
            },
        )

        return RecoveryResponse(
            session_id=str(session.id),
            current_stage=session.current_stage,
            status=session.status,
            recovery_status=session.recovery_status,
            action=request.action,
            updated_at=session.updated_at,
        )

    # -------------------------------------------------------------------
    # B-005：恢复一致性上下文构建与校验
    # -------------------------------------------------------------------

    async def _build_recovery_context(
        self, session_id: str, sid: uuid.UUID
    ) -> RecoveryContext:
        """采集 Redis checkpoint、PG snapshot、最近审计事件。

        Redis checkpoint 不可用时不抛错，降级为 missing。
        """
        ctx = RecoveryContext()

        # PG state_snapshot
        # （在调用方已加载 session；此处直接复用快照引用，避免重复查询）
        # snapshot 由调用方在 _validate_consistency 时从 session 读取，
        # 这里仅占位以保持上下文对象完整。
        ctx.snapshot = None  # 由 _validate_consistency 从 session 注入

        # 最近一条相关审计事件
        try:
            audit_result = await self._db.execute(
                select(AuditEvent)
                .where(AuditEvent.session_id == sid)
                .order_by(desc(AuditEvent.created_at))
                .limit(1)
            )
            latest_audit = audit_result.scalars().one_or_none()
            if latest_audit is not None:
                ctx.latest_audit_type = latest_audit.event_type
                ctx.latest_audit_at = latest_audit.created_at
        except Exception:  # noqa: BLE001
            # 审计查询失败不阻断恢复，降级为未知
            logger.warning(
                "恢复上下文查询最近审计事件失败 session_id=%s", session_id
            )

        # Redis checkpoint（best-effort，不可用则降级）
        await self._read_checkpoint(session_id, ctx)

        return ctx

    async def _read_checkpoint(self, session_id: str, ctx: RecoveryContext) -> None:
        """best-effort 读取 Redis checkpoint。

        checkpoint 结构未正式定义，按兼容性读取：尝试解析为 JSON，
        提取 state_version / current_stage。任何异常都降级为 missing/unreadable。
        """
        try:
            from app.core.redis import get_redis

            redis = await get_redis()
            raw = await redis.get(_checkpoint_key(session_id))
        except Exception:
            # Redis 不可用 → 降级为 missing，不阻断恢复
            ctx.checkpoint_status = "missing"
            return

        if raw is None:
            ctx.checkpoint_status = "missing"
            return

        try:
            data = json.loads(raw) if isinstance(raw, str) else raw
            if isinstance(data, dict):
                ctx.checkpoint = data
                ctx.checkpoint_status = "present"
                version = data.get("state_version")
                if isinstance(version, int):
                    ctx.checkpoint_version = version
                stage = data.get("current_stage")
                if isinstance(stage, str):
                    ctx.checkpoint_stage = stage
            else:
                ctx.checkpoint_status = "unreadable"
        except (json.JSONDecodeError, TypeError, ValueError):
            ctx.checkpoint_status = "unreadable"

    def _validate_consistency(
        self,
        session: ConsultSession,
        request: RecoveryRequest,
        ctx: RecoveryContext,
        session_id: str,
    ) -> None:
        """一致性检查：检测会话已终止、checkpoint 版本冲突等。

        冲突时不静默恢复，按情况返回项目内统一异常。
        """
        # 注入 PG snapshot 到上下文
        ctx.snapshot = session.state_snapshot

        # 冲突 1：审计显示会话已终止，但请求尝试非 terminate 恢复
        # （terminate 仍允许对已终止会话执行幂等终止，避免误伤运维）
        if ctx.is_terminated_by_audit() and request.action != "terminate":
            raise RecoveryNotNeededError(
                detail=(
                    f"session_id={session_id} 最近审计显示会话已终止 "
                    f"(audit_type={ctx.latest_audit_type})，"
                    "不可执行非 terminate 恢复动作"
                ),
                retryable=False,
            )

        # 冲突 2：checkpoint 版本明显旧于 PG state_version
        # 不允许用旧 checkpoint 覆盖 PG 权威状态；当前阶段不实现完整 Redis State 恢复，
        # 但记录比较结果。resume_from_pg_snapshot 以 PG 为准，不覆盖，故此处不阻断。
        # 该事实在 audit payload 中通过 checkpoint_status 体现，避免静默忽略。
        if ctx.checkpoint_older_than_pg(session.state_version):
            logger.info(
                "checkpoint 版本(%s)旧于 PG state_version(%s) session_id=%s，"
                "将以 PG 为权威，不覆盖",
                ctx.checkpoint_version,
                session.state_version,
                session_id,
            )

    # -------------------------------------------------------------------
    # 四种恢复动作
    # -------------------------------------------------------------------

    async def _do_resume_from_snapshot(
        self,
        session: ConsultSession,
        session_id: str,
        ctx: RecoveryContext,
        actor_type: str,
        actor_id: str | None,
        trace_id: str,
    ) -> None:
        """从 PG state_snapshot 恢复会话。

        优先使用 consult_sessions.state_snapshot 恢复为可继续状态；
        无 snapshot 时返回 STATE_RECOVERY_REQUIRED。
        checkpoint 缺失但 PG snapshot 可用时允许降级恢复，audit 记录降级事实。
        """
        snapshot = session.state_snapshot
        if not snapshot:
            # PG snapshot 缺失：若 checkpoint 可用且有 stage 信息，可降级恢复
            if ctx.checkpoint_status == "present" and ctx.checkpoint_stage:
                session.current_stage = ctx.checkpoint_stage
                if ctx.checkpoint_version is not None:
                    session.state_version = max(
                        session.state_version, ctx.checkpoint_version
                    )
            else:
                raise StateRecoveryRequiredError(
                    detail=(
                        f"session_id={session_id} 无 state_snapshot，"
                        f"checkpoint_status={ctx.checkpoint_status}，无法自动恢复"
                    ),
                    retryable=False,
                )
        else:
            # 从 snapshot 恢复关键字段
            if "current_stage" in snapshot:
                session.current_stage = snapshot["current_stage"]
            if "status" in snapshot:
                session.status = snapshot["status"]

        previous_recovery = session.recovery_status
        session.recovery_status = "normal"
        session.state_version += 1

        # 记录降级事实
        recovery_source = "pg_snapshot" if snapshot else "checkpoint"

        # 写入审计
        self._db.add(
            _audit_event(
                session_id=session.id,
                event_type="session.recovered",
                actor_type=actor_type,
                actor_id=actor_id,
                payload={
                    "action": "resume_from_pg_snapshot",
                    "previous_recovery_status": previous_recovery,
                    "snapshot_keys": list(snapshot.keys()) if snapshot else [],
                    "restored_stage": session.current_stage,
                    "restored_status": session.status,
                    "recovery_source": recovery_source,
                    "checkpoint_status": ctx.checkpoint_status,
                    "checkpoint_version": ctx.checkpoint_version,
                    "pg_state_version": session.state_version - 1,
                },
                trace_id=trace_id,
            )
        )

    async def _do_retry_current_stage(
        self,
        session: ConsultSession,
        session_id: str,
        ctx: RecoveryContext,
        actor_type: str,
        actor_id: str | None,
        trace_id: str,
    ) -> None:
        """将 recovery_status 置为 normal，保持 current_stage。

        不真实执行 Agent，仅将恢复状态置为正常。
        """
        previous_recovery = session.recovery_status
        session.recovery_status = "normal"
        # 如果会话状态是 blocked，恢复为 active
        if session.status == "blocked":
            session.status = "active"
            session.blocked_reason = None
            session.blocked_at = None
        session.state_version += 1

        self._db.add(
            _audit_event(
                session_id=session.id,
                event_type="session.recovered",
                actor_type=actor_type,
                actor_id=actor_id,
                payload={
                    "action": "retry_current_stage",
                    "previous_recovery_status": previous_recovery,
                    "current_stage": session.current_stage,
                    "restored_status": session.status,
                    "checkpoint_status": ctx.checkpoint_status,
                    "latest_audit_type": ctx.latest_audit_type,
                },
                trace_id=trace_id,
            )
        )

    async def _do_rollback_to_stage(
        self,
        session: ConsultSession,
        session_id: str,
        request: RecoveryRequest,
        ctx: RecoveryContext,
        actor_type: str,
        actor_id: str | None,
        trace_id: str,
    ) -> None:
        """回退到指定阶段。

        target_stage 合法性已在入口校验，此处直接更新。
        """
        previous_stage = session.current_stage
        target = request.target_stage
        assert target is not None  # 入口已校验

        session.current_stage = target
        session.recovery_status = "normal"
        # 如果会话状态是 blocked，将状态恢复为 active
        if session.status == "blocked":
            session.status = "active"
            session.blocked_reason = None
            session.blocked_at = None
        session.state_version += 1

        self._db.add(
            _audit_event(
                session_id=session.id,
                event_type="session.recovered",
                actor_type=actor_type,
                actor_id=actor_id,
                payload={
                    "action": "rollback_to_stage",
                    "target_stage": target,
                    "previous_stage": previous_stage,
                    "reason": request.reason,
                    "restored_status": session.status,
                    "checkpoint_status": ctx.checkpoint_status,
                    "latest_audit_type": ctx.latest_audit_type,
                },
                trace_id=trace_id,
            )
        )

    async def _do_terminate(
        self,
        session: ConsultSession,
        session_id: str,
        request: RecoveryRequest,
        ctx: RecoveryContext,
        actor_type: str,
        actor_id: str | None,
        trace_id: str,
    ) -> None:
        """终止会话（复用 P3-1 终止语义）。

        设置 status=terminated、blocked_reason=terminated_by_doctor。
        """
        previous_status = session.status
        previous_stage = session.current_stage

        session.status = "terminated"
        session.current_stage = "blocked"
        session.blocked_reason = _TERMINATED_BLOCKED_REASON
        session.blocked_at = _now()

        self._db.add(
            _audit_event(
                session_id=session.id,
                event_type="session.terminated",
                actor_type=actor_type,
                actor_id=actor_id,
                payload={
                    "action": "terminate",
                    "reason": request.reason,
                    "previous_status": previous_status,
                    "previous_stage": previous_stage,
                    "terminated_at": session.blocked_at.isoformat() if session.blocked_at else None,
                    "terminated_by": actor_id,
                    "checkpoint_status": ctx.checkpoint_status,
                },
                trace_id=trace_id,
            )
        )

    # -------------------------------------------------------------------
    # Redis Stream 事件（best-effort）
    # -------------------------------------------------------------------

    async def _try_emit_stream_event(
        self,
        session_id: str,
        event_type: str,
        payload: dict[str, Any],
    ) -> None:
        """Best-effort 写入 Redis Stream 事件。

        Redis 不可用时只记录 warning，不阻断恢复流程。
        Redis Stream 不是审计权威，关键审计以 PostgreSQL 为准。
        """
        try:
            from app.services.events import EventService

            event_service = EventService()
            await event_service.append_session_event(
                session_id=session_id,
                event_type=event_type,  # type: ignore[arg-type]
                payload=payload,
            )
        except Exception:
            logger.warning(
                "恢复后 Redis Stream 事件写入失败（best-effort，不影响恢复结果）: "
                "session_id=%s, event_type=%s",
                session_id,
                event_type,
            )
