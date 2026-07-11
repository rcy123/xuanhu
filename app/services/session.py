"""会话管理服务层。

提供创建、列表、详情、终止四个 P3-1 核心用例，并在同一事务中写入审计事件。
本层不调用 Agent、RAG、模型网关或会话锁。
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.exceptions import InvalidStageTransitionError, SessionNotFoundError
from app.models.audit import AuditEvent
from app.models.consult import ConsultSession
from app.schemas.session import (
    PatientInfo,
    SessionCreateRequest,
    SessionCreateResponse,
    SessionDetailResponse,
    SessionListItem,
    SessionListResponse,
    SessionTerminateRequest,
    SessionTerminateResponse,
)

# 可被终止的会话状态
_TERMINATABLE_STATUSES = {"active", "pending_review", "blocked"}

# 终止后进入的阻塞原因（与接口设计文档 §4.1.4 一致）
_TERMINATED_BLOCKED_REASON = "terminated_by_doctor"


def _now() -> datetime:
    """返回当前 UTC 时间（naive，与模型 blocked_at 列类型保持一致）。"""
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


class SessionService:
    """会话管理应用服务。"""

    def __init__(self, db: AsyncSession) -> None:
        self._db = db

    async def create_session(
        self,
        request: SessionCreateRequest,
        *,
        doctor_id: str | None,
        trace_id: str,
    ) -> SessionCreateResponse:
        """创建新问诊会话并写入 session.created 审计事件。"""
        patient_info_dict = request.patient_info.model_dump()
        patient_ref = request.patient_info.patient_ref
        agent_runtime = request.agent_runtime or get_settings().agent_runtime_version

        session = ConsultSession(
            patient_ref=patient_ref,
            patient_info=patient_info_dict,
            chief_complaint=request.chief_complaint,
            current_stage="inquiry",
            status="active",
            agent_runtime=agent_runtime,
            recovery_status="normal",
            state_version=1,
            rollback_counts={},
            created_by=doctor_id,
        )
        self._db.add(session)
        await self._db.flush()
        await self._db.refresh(session)

        actor_type = "doctor" if doctor_id else "system"
        audit = _audit_event(
            session_id=session.id,
            event_type="session.created",
            actor_type=actor_type,
            actor_id=doctor_id,
            payload={
                "patient_ref": patient_ref,
                "chief_complaint": request.chief_complaint,
                "initial_stage": "inquiry",
                "initial_status": "active",
                "agent_runtime": agent_runtime,
                "created_by": doctor_id,
            },
            trace_id=trace_id,
        )
        self._db.add(audit)

        return SessionCreateResponse(
            session_id=str(session.id),
            current_stage=session.current_stage,
            status=session.status,
            patient_info=PatientInfo.model_validate(session.patient_info),
            created_at=session.created_at,
        )

    async def list_sessions(
        self,
        *,
        status: str | None,
        patient_ref: str | None,
        page: int,
        page_size: int,
        sort: str,
    ) -> SessionListResponse:
        """查询会话列表，支持状态过滤、patient_ref 模糊搜索与分页排序。"""
        stmt = select(ConsultSession)

        if status is not None:
            stmt = stmt.where(ConsultSession.status == status)

        if patient_ref:
            stmt = stmt.where(ConsultSession.patient_ref.ilike(f"%{patient_ref}%"))

        if sort == "updated_at:desc":
            stmt = stmt.order_by(ConsultSession.updated_at.desc())
        else:
            # 默认 created_at:desc
            stmt = stmt.order_by(ConsultSession.created_at.desc())

        count_stmt = select(func.count()).select_from(stmt.subquery())
        total_result = await self._db.execute(count_stmt)
        total = total_result.scalar_one()

        stmt = stmt.offset((page - 1) * page_size).limit(page_size)
        result = await self._db.execute(stmt)
        sessions = result.scalars().all()

        items = [
            SessionListItem(
                session_id=str(s.id),
                patient_info=PatientInfo.model_validate(s.patient_info),
                chief_complaint=s.chief_complaint,
                current_stage=s.current_stage,
                status=s.status,
                pending_review=s.pending_review,
                created_by=s.created_by,
                created_at=s.created_at,
                updated_at=s.updated_at,
            )
            for s in sessions
        ]

        return SessionListResponse(
            items=items,
            total=total,
            page=page,
            page_size=page_size,
        )

    async def get_session(self, session_id: str, *, trace_id: str) -> SessionDetailResponse:
        """获取会话详情；不存在时返回 404 SESSION_NOT_FOUND。"""
        try:
            sid = uuid.UUID(session_id)
        except ValueError as exc:
            raise SessionNotFoundError(
                detail=f"session_id={session_id} 格式非法",
                retryable=False,
            ) from exc

        result = await self._db.execute(select(ConsultSession).where(ConsultSession.id == sid))
        session = result.scalar_one_or_none()
        if session is None:
            raise SessionNotFoundError(
                detail=f"session_id={session_id} 在数据库中未找到",
                retryable=False,
            )

        return SessionDetailResponse(
            session_id=str(session.id),
            status=session.status,
            current_stage=session.current_stage,
            pending_review=session.pending_review,
            recovery_status=session.recovery_status,
            blocked_reason=session.blocked_reason,
            rollback_counts=session.rollback_counts,
            state_version=session.state_version,
            patient_info=PatientInfo.model_validate(session.patient_info),
            chief_complaint=session.chief_complaint,
            created_at=session.created_at,
            updated_at=session.updated_at,
        )

    async def terminate_session(
        self,
        session_id: str,
        request: SessionTerminateRequest,
        *,
        doctor_id: str | None,
        trace_id: str,
    ) -> SessionTerminateResponse:
        """终止会话；仅允许 active/pending_review/blocked 状态。"""
        try:
            sid = uuid.UUID(session_id)
        except ValueError as exc:
            raise SessionNotFoundError(
                detail=f"session_id={session_id} 格式非法",
                retryable=False,
            ) from exc

        result = await self._db.execute(select(ConsultSession).where(ConsultSession.id == sid))
        session = result.scalar_one_or_none()
        if session is None:
            raise SessionNotFoundError(
                detail=f"session_id={session_id} 在数据库中未找到",
                retryable=False,
            )

        if session.status not in _TERMINATABLE_STATUSES:
            raise InvalidStageTransitionError(
                message=f"当前状态 {session.status} 不允许终止",
                detail=(
                    f"session_id={session_id} 状态为 {session.status}，"
                    "仅 active/pending_review/blocked 可终止"
                ),
                retryable=False,
            )

        previous_status = session.status
        session.status = "terminated"
        session.current_stage = "blocked"
        session.blocked_reason = _TERMINATED_BLOCKED_REASON
        session.blocked_at = _now()

        actor_type = "doctor" if doctor_id else "system"
        audit = _audit_event(
            session_id=session.id,
            event_type="session.terminated",
            actor_type=actor_type,
            actor_id=doctor_id,
            payload={
                "reason": request.reason,
                "previous_status": previous_status,
                "terminated_at": session.blocked_at.isoformat(),
                "terminated_by": doctor_id,
            },
            trace_id=trace_id,
        )
        self._db.add(audit)
        await self._db.flush()
        await self._db.refresh(session)

        return SessionTerminateResponse(
            session_id=str(session.id),
            status=session.status,
            current_stage=session.current_stage,
            blocked_reason=session.blocked_reason,
            updated_at=session.updated_at,
        )
