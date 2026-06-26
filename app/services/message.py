"""消息应用服务层。

提供消息提交和历史查询两个 P3-2 核心用例。
本层负责：
- 会话状态校验（存在性、terminated、inquiry-only）
- state_version 校验
- 获取/释放会话锁
- 消息持久化
- 审计事件写入
- state_version 递增与 state_snapshot 更新

本层不调用 Agent、RAG、模型网关。
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import (
    InvalidStageTransitionError,
    InvalidStateVersionError,
    SessionNotFoundError,
    SessionTerminatedError,
)
from app.models.audit import AuditEvent
from app.models.consult import ConsultMessage, ConsultSession
from app.schemas.message import (
    MessageCreateRequest,
    MessageCreateResponse,
    MessageItem,
    MessageListResponse,
)
from app.services.session_lock import SessionLock


def _now() -> datetime:
    """返回当前 UTC 时间（naive，与模型列默认值保持一致）。"""
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


class MessageService:
    """问诊消息应用服务。"""

    def __init__(self, db: AsyncSession) -> None:
        self._db = db

    # ------------------------------------------------------------------
    # 会话加载（与 SessionService 共享逻辑，P3-2 内联避免循环引用）
    # ------------------------------------------------------------------

    async def _load_session(self, session_id: str) -> ConsultSession:
        """加载会话；不存在或 ID 格式非法时抛出 SessionNotFoundError。"""
        try:
            sid = uuid.UUID(session_id)
        except ValueError as exc:
            raise SessionNotFoundError(
                detail=f"session_id={session_id} 格式非法",
                retryable=False,
            ) from exc

        result = await self._db.execute(
            select(ConsultSession).where(ConsultSession.id == sid)
        )
        session = result.scalar_one_or_none()
        if session is None:
            raise SessionNotFoundError(
                detail=f"session_id={session_id} 在数据库中未找到",
                retryable=False,
            )
        return session

    # ------------------------------------------------------------------
    # 提交问诊消息
    # ------------------------------------------------------------------

    async def submit_message(
        self,
        session_id: str,
        body: MessageCreateRequest,
        *,
        doctor_id: str | None,
        trace_id: str,
        x_state_version: int | None = None,
    ) -> MessageCreateResponse:
        """提交问诊消息。

        流程：
        1. 获取会话锁
        2. 校验会话状态（存在、未终止、current_stage=inquiry）
        3. 校验 state_version（如携带）
        4. 写入 consult_messages
        5. 写入 audit_events(message.created)
        6. 递增 state_version，更新 state_snapshot
        7. 释放锁并返回
        """
        # 1) 获取会话锁
        lock = SessionLock(self._db, session_id, trace_id)
        await lock.acquire()
        try:
            # 2) 加载并校验会话
            session = await self._load_session(session_id)

            if session.status == "terminated":
                raise SessionTerminatedError(
                    detail=f"session_id={session_id} 已终止，不可提交消息",
                    retryable=False,
                )

            if session.current_stage != "inquiry":
                raise InvalidStageTransitionError(
                    message=f"当前阶段 {session.current_stage} 不允许提交消息",
                    detail=(
                        f"session_id={session_id} 处于 {session.current_stage}，"
                        "仅 inquiry 阶段可提交消息"
                    ),
                    retryable=False,
                )

            # 3) state_version 校验
            if x_state_version is not None and x_state_version != session.state_version:
                raise InvalidStateVersionError(
                    detail=(
                        f"session_id={session_id} 客户端版本 {x_state_version} "
                        f"!= 服务端版本 {session.state_version}"
                    ),
                    retryable=True,
                )

            # 4) 写入消息
            sid = uuid.UUID(session_id)
            message = ConsultMessage(
                session_id=sid,
                role=body.role,
                stage=session.current_stage,
                content=body.content,
                trace_id=trace_id,
            )
            self._db.add(message)
            await self._db.flush()
            await self._db.refresh(message)

            # 5) 写入审计事件
            actor_type = "doctor" if doctor_id else "system"
            audit = _audit_event(
                session_id=sid,
                event_type="message.created",
                actor_type=actor_type,
                actor_id=doctor_id,
                payload={
                    "message_id": str(message.id),
                    "role": body.role,
                    "stage": session.current_stage,
                    "content_length": len(body.content),
                },
                trace_id=trace_id,
            )
            self._db.add(audit)

            # 6) 递增 state_version，更新 state_snapshot（可安全维护的消息摘要）
            session.state_version = session.state_version + 1
            snapshot = session.state_snapshot or {}
            # 安全维护：记录最后一条消息的角色和摘要，不伪造 Agent 结果
            snapshot["last_message"] = {
                "message_id": str(message.id),
                "role": body.role,
                "stage": session.current_stage,
                "preview": body.content[:200],
                "created_at": message.created_at.isoformat() if message.created_at else None,
            }
            session.state_snapshot = snapshot

            await self._db.flush()
            await self._db.refresh(session)

            return MessageCreateResponse(
                message_id=str(message.id),
                session_id=session_id,
                role=message.role,
                stage=message.stage,
                content=message.content,
                current_stage=session.current_stage,
                state_version=session.state_version,
                created_at=message.created_at,
            )
        finally:
            await lock.release()

    # ------------------------------------------------------------------
    # 查询消息历史
    # ------------------------------------------------------------------

    async def get_messages(
        self,
        session_id: str,
        *,
        before: str | None = None,
        limit: int = 50,
        stage: str | None = None,
        trace_id: str,
    ) -> MessageListResponse:
        """查询消息历史（游标分页，按 created_at desc）。

        参数：
            before: 游标 message_id，不传返回最新
            limit: 每页条数，默认 50，最大 100
            stage: 按阶段过滤
        """
        limit = min(max(limit, 1), 100)

        # 会话存在性校验
        await self._load_session(session_id)
        sid = uuid.UUID(session_id)

        stmt = select(ConsultMessage).where(ConsultMessage.session_id == sid)

        if stage is not None:
            stmt = stmt.where(ConsultMessage.stage == stage)

        # 游标分页：before 为 message_id，查询 created_at 早于它的消息
        if before is not None:
            try:
                before_id = uuid.UUID(before)
            except ValueError:
                # 无效游标 → 返回空结果
                return MessageListResponse(items=[], has_more=False, next_cursor=None)

            cursor_result = await self._db.execute(
                select(ConsultMessage.created_at).where(ConsultMessage.id == before_id)
            )
            cursor_ts = cursor_result.scalar_one_or_none()
            if cursor_ts is not None:
                # asyncpg 可能返回 tz-aware datetime，strip tzinfo 以匹配列类型
                if cursor_ts.tzinfo is not None:
                    cursor_ts = cursor_ts.replace(tzinfo=None)
                stmt = stmt.where(ConsultMessage.created_at < cursor_ts)

        # 按 created_at desc 排序，多取一条判断 has_more
        stmt = stmt.order_by(ConsultMessage.created_at.desc()).limit(limit + 1)

        result = await self._db.execute(stmt)
        rows = result.scalars().all()

        has_more = len(rows) > limit
        items = rows[:limit]

        message_items = [
            MessageItem(
                id=str(m.id),
                session_id=str(m.session_id),
                role=m.role,
                agent_name=m.agent_name,
                stage=m.stage,
                content=m.content,
                structured_delta=m.structured_delta,
                agent_run_id=str(m.agent_run_id) if m.agent_run_id else None,
                created_at=m.created_at,
            )
            for m in items
        ]

        next_cursor = str(items[-1].id) if has_more and items else None

        return MessageListResponse(
            items=message_items,
            has_more=has_more,
            next_cursor=next_cursor,
        )
