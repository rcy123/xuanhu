"""消息应用服务层。

提供消息提交和历史查询两个 P3-2 核心用例。
本层负责：
- 会话状态校验（存在性、terminated、inquiry-only）
- state_version 校验
- 获取/释放会话锁
- 消息持久化
- 审计事件写入
- state_version 递增与 state_snapshot 更新

P8-6: 在 inquiry 阶段保存医生消息后，触发 InquiryAgent + SufficiencyAgent，
将 Agent 回复落库为 role=agent/agent_name=inquiry 的 consult_messages，
写入 message.created 事件，更新 state_snapshot，返回 Agent 回复与完备性报告。

锁与事务模式（避免嵌套锁/死锁）：
- 段 A：持锁保存医生消息 → commit → release lock
- 段 B：重新持锁，运行 Agent、落库 Agent 消息、更新 state → commit → release lock
两段不嵌套；Agent 在段 B 内同步执行（HTTP 请求内）。

Agent 失败不伪造回复：段 B 失败时医生消息已落库，抛出 AgentTriggerFailedError。
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent_runtime.lifecycle import SharedLangGraphRuntime
from app.core.exceptions import (
    AgentTriggerFailedError,
    SessionNotFoundError,
)
from app.models.audit import AuditEvent
from app.models.consult import ConsultMessage, ConsultSession
from app.schemas.message import (
    MessageCreateRequest,
    MessageCreateResponse,
    MessageItem,
    MessageListResponse,
)
from app.services.events import EventService
from app.services.langgraph_intake import LangGraphIntakeMessageRunner

logger = logging.getLogger("xuanhu.message")


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

    def __init__(
        self,
        db: AsyncSession,
        *,
        event_service: EventService | None = None,
        shared_langgraph_runtime: SharedLangGraphRuntime | None = None,
        allow_request_local_langgraph_runtime: bool = False,
    ) -> None:
        self._db = db
        self._event_service = event_service
        self._shared_langgraph_runtime = shared_langgraph_runtime
        self._allow_request_local_langgraph_runtime = allow_request_local_langgraph_runtime

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

        result = await self._db.execute(select(ConsultSession).where(ConsultSession.id == sid))
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
        idempotency_key: str | None = None,
    ) -> MessageCreateResponse:
        """提交问诊消息。

        流程（接口设计文档 §4.2.1）：
        段 A（持锁）：
          1. 校验会话状态（存在、未终止、current_stage=inquiry）
          2. 校验 state_version（如携带）
          3. 写入 consult_messages（医生消息）
          4. 写入 audit_events(message.created)
          5. 递增 state_version，更新 state_snapshot
          6. commit + 释放锁
        段 B（重新持锁）：
          7. 重建 XuanhuState（注入新医生消息到 inquiry_messages）
          8. 调 InquiryAgent → 落库 Agent 消息（role=agent/agent_name=inquiry）
          9. 调 SufficiencyAgent → 合并到 state_snapshot
          10. 递增 state_version，写 audit + message.created 事件
          11. commit + 释放锁
        """
        session = await self._load_session(session_id)
        if getattr(session, "agent_runtime", "legacy") == "langgraph":
            self._require_langgraph_runtime()
            return await LangGraphIntakeMessageRunner(
                self._db,
                event_service=self._event_service,
                shared_runtime=self._shared_langgraph_runtime,
                allow_request_local_runtime=(self._allow_request_local_langgraph_runtime),
            ).submit_message(
                session_id,
                body,
                doctor_id=doctor_id,
                trace_id=trace_id,
                x_state_version=x_state_version,
                idempotency_key=idempotency_key,
            )

        # 3d: legacy 路径已下线——历史 legacy session 仅兼容读,不再受理新消息。
        raise AgentTriggerFailedError(
            detail=f"session_id={session_id} legacy runtime has been decommissioned; session is read-only",
            agent_error_code="LEGACY_RUNTIME_DECOMMISSIONED",
            retryable=False,
        )

    async def ensure_submission_runtime_available(self, session_id: str) -> bool:
        """Fail before HTTP idempotency claims when LangGraph is unavailable."""

        session = await self._load_session(session_id)
        if getattr(session, "agent_runtime", "legacy") == "langgraph":
            self._require_langgraph_runtime()
            return True
        return False

    def _require_langgraph_runtime(self) -> None:
        if self._shared_langgraph_runtime is None and not self._allow_request_local_langgraph_runtime:
            raise AgentTriggerFailedError(
                detail="shared LangGraph runtime is unavailable",
                agent_error_code="LANGGRAPH_RUNTIME_UNAVAILABLE",
                retryable=True,
            )

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

        stmt = select(ConsultMessage).where(
            ConsultMessage.session_id == sid,
            ~((ConsultMessage.role == "system") & (ConsultMessage.agent_name == "initial_domain_seed")),
        )

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


# 静默导入：保持 BaseModel 类型引用供 mypy
_BM = BaseModel  # noqa: F841
