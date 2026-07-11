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

from app.agents.base import AgentResult, BaseAgent
from app.agents.errors import AgentRunError
from app.agents.inquiry import InquiryAgent, merge_inquiry_output_to_state
from app.agents.registry import AgentRegistry
from app.agents.sufficiency import SufficiencyAgent
from app.core.exceptions import (
    AgentTriggerFailedError,
    InvalidStageTransitionError,
    InvalidStateVersionError,
    SessionNotFoundError,
    SessionTerminatedError,
)
from app.models.audit import AuditEvent
from app.models.consult import ConsultMessage, ConsultSession
from app.schemas.agent import (
    InquiryAgentOutput,
    SufficiencyReport,
    XuanhuState,
)
from app.schemas.message import (
    AgentMessageItem,
    MessageCreateRequest,
    MessageCreateResponse,
    MessageItem,
    MessageListResponse,
    SufficiencyReportData,
)
from app.schemas.types import Stage
from app.services.events import EventService
from app.services.langgraph_intake import LangGraphIntakeMessageRunner
from app.services.session_lock import SessionLock

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


def _default_inquiry_registry() -> AgentRegistry:
    """构造 inquiry 阶段所需 Agent 注册表（InquiryAgent + SufficiencyAgent）。

    与 Supervisor 的全量 registry 区分：本服务只覆盖 §4.2.1 inquiry 流所需 Agent，
    不引入 syndrome/prescription/... 等阶段 Agent，避免越权推进。
    """
    registry = AgentRegistry()
    registry.register(Stage.INQUIRY, InquiryAgent())  # type: ignore[arg-type]
    registry.register(Stage.SUFFICIENCY, SufficiencyAgent())  # type: ignore[arg-type]
    return registry


class MessageService:
    """问诊消息应用服务。"""

    def __init__(
        self,
        db: AsyncSession,
        *,
        registry: AgentRegistry | None = None,
        event_service: EventService | None = None,
        inquiry_agent: BaseAgent | None = None,
        sufficiency_agent: BaseAgent | None = None,
    ) -> None:
        self._db = db
        self._registry = registry or _default_inquiry_registry()
        self._event_service = event_service
        # 支持测试/试用注入 fake agent，绕过真实模型网关。
        # 生产路径留空，走 _registry 的真实 InquiryAgent/SufficiencyAgent。
        self._inquiry_agent_override = inquiry_agent
        self._sufficiency_agent_override = sufficiency_agent

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
            return await LangGraphIntakeMessageRunner(
                self._db,
                event_service=self._event_service,
            ).submit_message(
                session_id,
                body,
                doctor_id=doctor_id,
                trace_id=trace_id,
                x_state_version=x_state_version,
            )

        # 段 A：保存医生消息
        doctor_message, session = await self._save_doctor_message_locked(
            session_id,
            body,
            doctor_id=doctor_id,
            trace_id=trace_id,
            x_state_version=x_state_version,
        )

        # 段 B：触发 Agent（失败抛 AgentTriggerFailedError，医生消息已落库）
        agent_msg, sufficiency, final_state_version = await self._run_inquiry_agents_locked(
            session_id,
            doctor_message,
            doctor_id=doctor_id,
            trace_id=trace_id,
        )

        return MessageCreateResponse(
            message_id=str(doctor_message.id),
            session_id=session_id,
            role=doctor_message.role,
            stage=doctor_message.stage,
            content=doctor_message.content,
            current_stage=session.current_stage,
            state_version=final_state_version,
            created_at=doctor_message.created_at,
            agent_message=agent_msg,
            sufficiency_report=sufficiency,
        )

    async def _save_doctor_message_locked(
        self,
        session_id: str,
        body: MessageCreateRequest,
        *,
        doctor_id: str | None,
        trace_id: str,
        x_state_version: int | None = None,
    ) -> tuple[ConsultMessage, ConsultSession]:
        """段 A：持锁保存医生消息并提交。"""
        lock = SessionLock(self._db, session_id, trace_id)
        await lock.acquire()
        try:
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

            if x_state_version is not None and x_state_version != session.state_version:
                raise InvalidStateVersionError(
                    detail=(
                        f"session_id={session_id} 客户端版本 {x_state_version} "
                        f"!= 服务端版本 {session.state_version}"
                    ),
                    retryable=True,
                )

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

            session.state_version = session.state_version + 1
            snapshot = session.state_snapshot or {}
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

            await self._append_message_created_event(session_id, message)

            await self._db.commit()
            return message, session
        finally:
            await lock.release()

    async def _run_inquiry_agents_locked(
        self,
        session_id: str,
        doctor_message: ConsultMessage,
        *,
        doctor_id: str | None,
        trace_id: str,
    ) -> tuple[AgentMessageItem | None, SufficiencyReportData | None, int]:
        """段 B：重新持锁，运行 InquiryAgent + SufficiencyAgent，落库 Agent 消息。

        Agent 失败时不伪造回复：写 agent.failed 审计，抛 AgentTriggerFailedError。
        医生消息在段 A 已 commit，不会回滚。
        """
        lock = SessionLock(self._db, session_id, trace_id)
        await lock.acquire()
        try:
            session = await self._load_session(session_id)
            sid = uuid.UUID(session_id)

            # 重建 XuanhuState，并注入本轮医生消息到 inquiry_messages
            state = self._build_state_from_session(session, trace_id)
            state = self._inject_doctor_message(state, doctor_message)

            # 调用 InquiryAgent
            inquiry_agent = self._inquiry_agent_override or self._registry.get(Stage.INQUIRY)
            if inquiry_agent is None:
                raise AgentTriggerFailedError(
                    detail=f"session_id={session_id} InquiryAgent 未注册",
                    agent_error_code="INQUIRY_AGENT_MISSING",
                )

            inquiry_result = await self._run_agent(inquiry_agent, state, trace_id)
            inquiry_output = inquiry_result.output
            if not isinstance(inquiry_output, InquiryAgentOutput):
                raise AgentTriggerFailedError(
                    detail=(
                        f"session_id={session_id} InquiryAgent 输出类型非法: "
                        f"{type(inquiry_output).__name__}"
                    ),
                    agent_error_code="INQUIRY_OUTPUT_INVALID",
                )

            # 合并问诊增量到 state
            updates = merge_inquiry_output_to_state(state, inquiry_output)
            state = state.model_copy(update=updates)

            # 落库 Agent 消息
            agent_message = ConsultMessage(
                session_id=sid,
                role="agent",
                agent_name="inquiry",
                stage=session.current_stage,
                content=inquiry_output.next_question,
                agent_run_id=(
                    uuid.UUID(inquiry_result.agent_run_id)
                    if inquiry_result.agent_run_id
                    else None
                ),
                structured_delta=inquiry_output.model_dump(mode="json", exclude_none=True),
                trace_id=trace_id,
            )
            self._db.add(agent_message)
            await self._db.flush()
            await self._db.refresh(agent_message)

            # 调用 SufficiencyAgent
            sufficiency_agent = self._sufficiency_agent_override or self._registry.get(Stage.SUFFICIENCY)
            sufficiency_report: SufficiencyReport | None = None
            if sufficiency_agent is not None:
                suff_result = await self._run_agent(sufficiency_agent, state, trace_id)
                suff_output = suff_result.output
                if isinstance(suff_output, SufficiencyReport):
                    sufficiency_report = suff_output
                    state = state.model_copy(
                        update={"sufficiency_report": suff_output}
                    )

            # 更新 state_snapshot + state_version
            session.state_version = session.state_version + 1
            session.state_snapshot = self._merge_state_to_snapshot(session, state)

            # audit: message.created (agent)
            self._db.add(
                _audit_event(
                    session_id=sid,
                    event_type="message.created",
                    actor_type="agent",
                    actor_id="inquiry",
                    payload={
                        "message_id": str(agent_message.id),
                        "role": "agent",
                        "agent_name": "inquiry",
                        "stage": session.current_stage,
                        "agent_run_id": (
                            str(agent_message.agent_run_id)
                            if agent_message.agent_run_id
                            else None
                        ),
                        "content_length": len(agent_message.content),
                    },
                    trace_id=trace_id,
                )
            )

            await self._db.flush()
            await self._db.refresh(session)

            # Redis Stream 事件（agent 消息）
            await self._append_message_created_event(session_id, agent_message)

            await self._db.commit()

            agent_item = AgentMessageItem(
                message_id=str(agent_message.id),
                role="agent",
                agent_name="inquiry",
                stage=agent_message.stage,
                content=agent_message.content,
                agent_run_id=(
                    str(agent_message.agent_run_id)
                    if agent_message.agent_run_id
                    else None
                ),
                created_at=agent_message.created_at,
            )

            suff_data: SufficiencyReportData | None = None
            if sufficiency_report is not None:
                suff_data = SufficiencyReportData(
                    sufficient=sufficiency_report.sufficient,
                    covered=list(sufficiency_report.covered),
                    missing=list(sufficiency_report.missing),
                    suggestions=list(sufficiency_report.suggestions),
                )

            return agent_item, suff_data, session.state_version
        except AgentTriggerFailedError:
            # 已在内部记录，向上抛出
            raise
        except AgentRunError as exc:
            await self._db.rollback()
            await self._record_agent_failed(session_id, "inquiry", exc, trace_id)
            raise AgentTriggerFailedError(
                detail=(
                    f"session_id={session_id} Agent 执行失败 code={exc.code} "
                    f"retryable={exc.retryable}"
                ),
                agent_error_code=exc.code,
                retryable=exc.retryable,
            ) from exc
        except Exception as exc:
            await self._db.rollback()
            await self._record_agent_failed(session_id, "inquiry", exc, trace_id)
            raise AgentTriggerFailedError(
                detail=(
                    f"session_id={session_id} Agent 触发异常: "
                    f"{type(exc).__name__}: {exc}"
                ),
                agent_error_code="AGENT_TRIGGER_EXCEPTION",
                retryable=False,
            ) from exc
        finally:
            await lock.release()

    async def _run_agent(
        self,
        agent: BaseAgent,
        state: XuanhuState,
        trace_id: str,
    ) -> AgentResult:
        """运行 Agent，返回 AgentResult。AgentRunError 由调用方处理。"""
        return await agent.run(state, trace_id)

    async def _record_agent_failed(
        self,
        session_id: str,
        agent_name: str,
        exc: Exception,
        trace_id: str,
    ) -> None:
        """记录 agent.failed 审计事件（best-effort，独立事务）。"""
        try:
            sid = uuid.UUID(session_id)
        except (ValueError, AttributeError):
            return
        try:
            self._db.add(
                _audit_event(
                    session_id=sid,
                    event_type="agent.failed",
                    actor_type="agent",
                    actor_id=agent_name,
                    payload={
                        "agent_name": agent_name,
                        "error_code": getattr(exc, "code", "AGENT_TRIGGER_EXCEPTION"),
                        "retryable": getattr(exc, "retryable", False),
                        "trace_id": trace_id,
                    },
                    trace_id=trace_id,
                )
            )
            await self._db.commit()
        except Exception:  # noqa: BLE001
            logger.warning(
                "agent.failed 审计写入失败 session=%s agent=%s",
                session_id,
                agent_name,
                exc_info=True,
            )

    # ------------------------------------------------------------------
    # State 重建与合并
    # ------------------------------------------------------------------

    def _build_state_from_session(
        self,
        session: ConsultSession,
        trace_id: str,
    ) -> XuanhuState:
        """从 consult_sessions.state_snapshot 重建 XuanhuState。"""
        snapshot = session.state_snapshot or {}
        current_stage = snapshot.get("current_stage", session.current_stage)
        if isinstance(current_stage, str):
            current_stage = Stage(current_stage)
        data: dict[str, Any] = {
            "session_id": str(session.id),
            "current_stage": current_stage,
            "pending_review": snapshot.get("pending_review", session.pending_review),
            "rollback_counts": snapshot.get(
                "rollback_counts", dict(session.rollback_counts or {})
            ),
            "blocked_reason": snapshot.get("blocked_reason", session.blocked_reason),
            "state_version": session.state_version,
            "recovery_status": snapshot.get(
                "recovery_status", session.recovery_status or "normal"
            ),
            "trace_id": trace_id,
        }
        for key in (
            "patient_info",
            "chief_complaint",
            "present_illness",
            "past_history",
            "personal_family_history",
            "ten_questions",
            "four_diagnosis",
            "inquiry_messages",
            "evidences",
            "sufficiency_report",
            "syndrome_result",
            "base_formula",
            "modified_formula",
            "safety_rule_result",
            "safety_review",
            "doctor_review",
            "medical_record",
        ):
            if key in snapshot:
                data[key] = snapshot[key]
        return XuanhuState.model_validate(data)

    def _inject_doctor_message(
        self,
        state: XuanhuState,
        doctor_message: ConsultMessage,
    ) -> XuanhuState:
        """将本轮医生消息注入 inquiry_messages，供 InquiryAgent prompt 使用。"""
        new_msg: dict[str, Any] = {
            "role": doctor_message.role,
            "content": doctor_message.content,
        }
        return state.model_copy(
            update={
                "inquiry_messages": list(state.inquiry_messages) + [new_msg],
            }
        )

    def _merge_state_to_snapshot(
        self,
        session: ConsultSession,
        state: XuanhuState,
    ) -> dict[str, Any]:
        """将 XuanhuState 合并回 state_snapshot（保留 last_message 等已有字段）。"""
        existing = session.state_snapshot or {}
        snapshot = state.model_dump(mode="python")
        keys = {
            "session_id",
            "patient_info",
            "chief_complaint",
            "present_illness",
            "past_history",
            "personal_family_history",
            "ten_questions",
            "four_diagnosis",
            "inquiry_messages",
            "evidences",
            "sufficiency_report",
            "syndrome_result",
            "base_formula",
            "modified_formula",
            "safety_rule_result",
            "safety_review",
            "doctor_review",
            "medical_record",
            "current_stage",
            "pending_review",
            "rollback_counts",
            "blocked_reason",
            "state_version",
            "recovery_status",
            "trace_id",
        }
        merged: dict[str, Any] = dict(existing)
        for k, v in snapshot.items():
            if k in keys and v is not None:
                merged[k] = v
        return merged

    async def _append_message_created_event(
        self,
        session_id: str,
        message: ConsultMessage,
    ) -> None:
        """写入 message.created Redis Stream 事件；失败不影响权威 DB 事务。"""
        try:
            await (self._event_service or EventService()).append_session_event(
                session_id,
                "message.created",
                {
                    "message_id": str(message.id),
                    "role": message.role,
                    "agent_name": message.agent_name,
                    "stage": message.stage,
                    "content": message.content,
                    "structured_delta": message.structured_delta,
                    "agent_run_id": str(message.agent_run_id) if message.agent_run_id else None,
                    "created_at": message.created_at.isoformat() if message.created_at else None,
                },
            )
        except Exception:  # noqa: BLE001
            logger.warning(
                "message.created 事件写入 Redis Stream 失败 session=%s message=%s",
                session_id,
                message.id,
                exc_info=True,
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


# 静默导入：保持 BaseModel 类型引用供 mypy
_BM = BaseModel  # noqa: F841
