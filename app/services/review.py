"""医师确认服务层。

实现 P7-1 医师确认 API 的核心业务逻辑：
- confirm：确认安全审核通过的处方，推进到 record 阶段
- modify：修改处方 → 二次安全审核 → 通过后推进到 record
- reject：否决处方，回退到 prescription 阶段

所有路径均写入 doctor_reviews 和 audit_events。
本层不实现病历生成（P7-2）、病历编辑或导出。
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import (
    FormulaOverrideRequiredError,
    InvalidReviewActionError,
    InvalidStageTransitionError,
    InvalidStateVersionError,
    SafetyAcceptRiskUnsupportedError,
    SafetyReviewBlockedError,
    SessionNotFoundError,
    SessionTerminatedError,
)
from app.models.audit import AuditEvent
from app.models.consult import ConsultSession
from app.models.review import DoctorReview
from app.schemas.agent import (
    FormulaResult,
    HerbDose,
    SafetyReview,
    XuanhuState,
)
from app.schemas.review import (
    ReviewRequest,
    ReviewResponse,
)
from app.schemas.types import RollbackTarget, Severity
from app.services.events import EventService
from app.services.session_lock import SessionLock

logger = logging.getLogger("xuanhu.review")

# 合法的 review action 值
_VALID_ACTIONS = {"confirm", "modify", "reject"}

# 默认方名（当医师修改处方未提供 name 时）
_DEFAULT_OVERRIDE_FORMULA_NAME = "医师修改方"


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


class ReviewService:
    """医师确认应用服务。"""

    def __init__(self, db: AsyncSession) -> None:
        self._db = db

    # ------------------------------------------------------------------
    # 公开入口
    # ------------------------------------------------------------------

    async def review(
        self,
        session_id: str,
        request: ReviewRequest,
        *,
        doctor_id: str | None,
        trace_id: str,
        x_state_version: int | None = None,
    ) -> ReviewResponse:
        """执行医师确认动作。

        流程：
        1. 获取 SessionLock
        2. 校验会话状态（存在、未终止、current_stage=review + pending_review=true）
        3. 校验 state_version
        4. 从 state_snapshot 重建 State
        5. 按 action 分发到 _do_confirm / _do_modify / _do_reject
        6. 写入 audit_events
        7. 释放锁并返回

        Raises:
            SessionNotFoundError: 会话不存在。
            SessionTerminatedError: 会话已终止。
            InvalidStageTransitionError: 当前阶段非 review 或无待确认处方。
            InvalidStateVersionError: state_version 冲突。
            InvalidReviewActionError: 无效 action。
            FormulaOverrideRequiredError: modify 缺少 formula_override。
            SafetyReviewBlockedError: modify 二次安全审核阻断。
            SafetyAcceptRiskUnsupportedError: MVP 不支持接受风险。
            SessionBusyError: 会话锁冲突。
        """
        lock = SessionLock(self._db, session_id, trace_id)
        await lock.acquire()
        try:
            return await self._review_locked(
                session_id, request, doctor_id, trace_id, x_state_version
            )
        finally:
            await lock.release()

    # ------------------------------------------------------------------
    # 内部实现
    # ------------------------------------------------------------------

    async def _review_locked(
        self,
        session_id: str,
        request: ReviewRequest,
        doctor_id: str | None,
        trace_id: str,
        x_state_version: int | None,
    ) -> ReviewResponse:
        """在已持锁状态下执行 review 动作。"""
        # 1. 加载会话
        session = await self._load_session(session_id)

        # 2. 校验 terminated
        if session.status == "terminated":
            raise SessionTerminatedError(
                detail=f"session_id={session_id} 已终止，不可执行医师确认",
                retryable=False,
            )

        # 3. 校验 current_stage + pending_review
        if session.current_stage != "review" or not session.pending_review:
            raise InvalidStageTransitionError(
                message="当前无待确认处方",
                detail=(
                    f"session_id={session_id} current_stage={session.current_stage} "
                    f"pending_review={session.pending_review}，"
                    "仅 review 阶段且 pending_review=true 时可执行医师确认"
                ),
                retryable=False,
            )

        # 4. 校验 state_version
        if x_state_version is not None and x_state_version != session.state_version:
            raise InvalidStateVersionError(
                detail=(
                    f"session_id={session_id} 客户端版本 {x_state_version} "
                    f"!= 服务端版本 {session.state_version}"
                ),
                retryable=True,
            )

        # 5. 校验 action
        action = request.action
        if action not in _VALID_ACTIONS:
            raise InvalidReviewActionError(
                message=f"无效的 review action: {action}",
                detail=(
                    f"session_id={session_id} action={action}，"
                    f"仅支持 {sorted(_VALID_ACTIONS)}"
                ),
                retryable=False,
            )

        # 6. 从 state_snapshot 重建关键信息
        snapshot = session.state_snapshot or {}
        state = self._build_state_from_snapshot(snapshot, str(session.id))

        # 7. 按 action 分发
        if action == "confirm":
            return await self._do_confirm(
                session, session_id, state, snapshot, request, doctor_id, trace_id
            )
        elif action == "modify":
            return await self._do_modify(
                session, session_id, state, snapshot, request, doctor_id, trace_id
            )
        else:  # reject
            return await self._do_reject(
                session, session_id, state, snapshot, request, doctor_id, trace_id
            )

    # ------------------------------------------------------------------
    # confirm
    # ------------------------------------------------------------------

    async def _do_confirm(
        self,
        session: ConsultSession,
        session_id: str,
        state: XuanhuState,
        snapshot: dict[str, Any],
        request: ReviewRequest,
        doctor_id: str | None,
        trace_id: str,
    ) -> ReviewResponse:
        """确认处方：推进到 record 阶段。"""
        safety_review = state.safety_review
        safety_rule_result = state.safety_rule_result

        # 1. 防御性检查：必须有安全审核结果
        if safety_review is None or safety_rule_result is None:
            raise InvalidStageTransitionError(
                message="安全审核结果缺失，无法确认处方",
                detail=(
                    f"session_id={session_id} safety_review={safety_review is not None} "
                    f"safety_rule_result={safety_rule_result is not None}"
                ),
                retryable=False,
            )

        # 2. 安全审核必须通过
        if not safety_review.passed:
            raise InvalidStageTransitionError(
                message="安全审核未通过，不可确认处方",
                detail=(
                    f"session_id={session_id} safety_review.passed={safety_review.passed}，"
                    "请先处理安全问题或回退重新开方"
                ),
                retryable=False,
            )

        # 3. MVP 不接受 blocker/high 问题
        blocker_high = [
            i for i in safety_review.issues
            if i.severity in (Severity.BLOCKER, Severity.HIGH)
        ]
        if blocker_high:
            raise SafetyAcceptRiskUnsupportedError(
                detail=(
                    f"session_id={session_id} 存在 {len(blocker_high)} 个 "
                    "blocker/high 安全问题，MVP 不可接受风险继续"
                ),
                retryable=False,
            )

        # 4. 提取原处方和 safety_rule_run_id
        original_formula = self._extract_formula_dict(state)
        sid = uuid.UUID(session_id)

        # 查找最新的 safety_rule_run_id（来自 P6-3/P6-4 的 agent_output 安全审核）
        safety_rule_run_id = await self._get_latest_safety_rule_run_id(sid)

        # 5. 插入 DoctorReview
        review_record = DoctorReview(
            session_id=sid,
            agent_run_id=None,  # P7-1 不关联 agent_run
            safety_rule_run_id=safety_rule_run_id,
            action="confirm",
            original_formula=original_formula,
            formula_override=None,
            feedback=None,
            reviewed_by=doctor_id,
        )
        self._db.add(review_record)
        await self._db.flush()
        await self._db.refresh(review_record)

        # 6. 构造 doctor_review dict
        doctor_review_dict: dict[str, Any] = {
            "action": "confirm",
            "reviewed_by": doctor_id,
            "reviewed_at": _now().isoformat(),
            "review_id": str(review_record.id),
        }

        # 7. 更新 state_snapshot + session
        # B-013: P7-1 不生成病历，不得将 status 置为 done（done 语义为"病历已生成"）。
        # confirm 后进入 record 阶段，status 保持 active，供 P7-2 病历生成接续。
        new_snapshot = dict(snapshot)
        new_snapshot["doctor_review"] = doctor_review_dict
        new_snapshot["current_stage"] = "record"
        new_snapshot["pending_review"] = False

        session.current_stage = "record"
        session.status = "active"
        session.pending_review = False
        session.state_version += 1
        session.state_snapshot = new_snapshot

        # 8. 写审计
        self._db.add(
            _audit_event(
                session_id=sid,
                event_type="doctor.reviewed",
                actor_type="doctor" if doctor_id else "system",
                actor_id=doctor_id,
                payload={
                    "action": "confirm",
                    "review_id": str(review_record.id),
                    "safety_rule_run_id": str(safety_rule_run_id) if safety_rule_run_id else None,
                    "state_version": session.state_version,
                    "to_stage": "record",
                    "trace_id": trace_id,
                },
                trace_id=trace_id,
            )
        )

        await self._db.flush()
        await self._db.refresh(session)

        await self._try_emit_stream_event(
            session_id=str(session.id),
            event_type="doctor.reviewed",
            payload={
                "action": "confirm",
                "review_id": str(review_record.id),
                "to_stage": "record",
                "state_version": session.state_version,
            },
        )

        return ReviewResponse(
            session_id=str(session.id),
            action="confirm",
            current_stage=session.current_stage,
            status=session.status,
            pending_review=session.pending_review,
            review_id=str(review_record.id),
            state_version=session.state_version,
            original_formula=original_formula,
            updated_at=session.updated_at,
        )

    # ------------------------------------------------------------------
    # modify
    # ------------------------------------------------------------------

    async def _do_modify(
        self,
        session: ConsultSession,
        session_id: str,
        state: XuanhuState,
        snapshot: dict[str, Any],
        request: ReviewRequest,
        doctor_id: str | None,
        trace_id: str,
    ) -> ReviewResponse:
        """修改处方：二次安全审核 → 通过后推进到 record。"""
        # 1. 校验 formula_override
        if request.formula_override is None:
            raise FormulaOverrideRequiredError(
                detail=f"session_id={session_id} action=modify 但未提供 formula_override",
                retryable=False,
            )

        override = request.formula_override

        # 2. 从 formula_override 构造 FormulaResult
        override_formula = FormulaResult(
            name=override.name or _DEFAULT_OVERRIDE_FORMULA_NAME,
            composition=[
                HerbDose(
                    herb=h.herb,
                    dose=h.dose,
                    unit=h.unit,
                    note=h.note,
                )
                for h in override.composition
            ],
            source=override.source,
            rationale=override.rationale or "医师修改处方",
        )

        # 3. 获取患者信息
        patient_info = state.patient_info

        # 4. 二次安全审核
        from app.safety.engine import SafetyRuleEngine

        engine = SafetyRuleEngine(self._db)
        rule_result = await engine.check(
            formula=override_formula,
            patient_info=patient_info,
            session_id=session_id,
            trace_id=trace_id,
            formula_source="doctor_override",
        )

        # 5. 生成 SafetyReview
        safety_review = self._rule_result_to_safety_review(rule_result)

        # 6. 阻断检查
        if not safety_review.passed:
            # 不写 doctor_reviews，直接返回安全问题
            raise SafetyReviewBlockedError(
                message="医师修改处方后二次安全审核未通过",
                detail=(
                    f"session_id={session_id} modify 二次安全审核 "
                    f"found {len(rule_result.issues)} issue(s)"
                ),
                retryable=False,
                issues=[i.model_dump(mode="json") for i in rule_result.issues],
            )

        # 7. 通过 → 写入 DoctorReview
        original_formula = self._extract_formula_dict(state)
        sid = uuid.UUID(session_id)
        override_dict = override.model_dump(mode="json")

        # 查找最新 safety_rule_run（由 SafetyRuleEngine.check() 在上一步写入）
        new_safety_rule_run_id = await self._get_latest_safety_rule_run_id(sid)

        review_record = DoctorReview(
            session_id=sid,
            agent_run_id=None,
            safety_rule_run_id=new_safety_rule_run_id,
            action="modify",
            original_formula=original_formula,
            formula_override=override_dict,
            feedback=request.feedback,
            reviewed_by=doctor_id,
        )
        self._db.add(review_record)
        await self._db.flush()
        await self._db.refresh(review_record)

        # 8. 构造 doctor_review dict
        doctor_review_dict: dict[str, Any] = {
            "action": "modify",
            "reviewed_by": doctor_id,
            "reviewed_at": _now().isoformat(),
            "review_id": str(review_record.id),
            "formula_override": override_dict,
            "feedback": request.feedback,
        }

        # 9. 更新 state_snapshot
        # B-013: P7-1 不生成病历，不得将 status 置为 done。
        # modify 后进入 record 阶段，status 保持 active，供 P7-2 病历生成接续。
        new_snapshot = dict(snapshot)
        new_snapshot["doctor_review"] = doctor_review_dict
        new_snapshot["modified_formula"] = {
            "formula": override_formula.model_dump(mode="python"),
            "modifications": [],
        }
        new_snapshot["safety_rule_result"] = rule_result.model_dump(mode="python")
        new_snapshot["safety_review"] = safety_review.model_dump(mode="python")
        new_snapshot["current_stage"] = "record"
        new_snapshot["pending_review"] = False

        session.current_stage = "record"
        session.status = "active"
        session.pending_review = False
        session.state_version += 1
        session.state_snapshot = new_snapshot

        # 10. 写审计
        self._db.add(
            _audit_event(
                session_id=sid,
                event_type="doctor.reviewed",
                actor_type="doctor" if doctor_id else "system",
                actor_id=doctor_id,
                payload={
                    "action": "modify",
                    "review_id": str(review_record.id),
                    "safety_rule_run_id": (
                        str(new_safety_rule_run_id) if new_safety_rule_run_id else None
                    ),
                    "state_version": session.state_version,
                    "to_stage": "record",
                    "trace_id": trace_id,
                },
                trace_id=trace_id,
            )
        )

        await self._db.flush()
        await self._db.refresh(session)

        await self._try_emit_stream_event(
            session_id=str(session.id),
            event_type="doctor.reviewed",
            payload={
                "action": "modify",
                "review_id": str(review_record.id),
                "to_stage": "record",
                "state_version": session.state_version,
            },
        )

        return ReviewResponse(
            session_id=str(session.id),
            action="modify",
            current_stage=session.current_stage,
            status=session.status,
            pending_review=session.pending_review,
            review_id=str(review_record.id),
            state_version=session.state_version,
            original_formula=original_formula,
            formula_override=override_dict,
            feedback=request.feedback,
            safety_recheck={
                "passed": True,
                "issues": [],
            },
            updated_at=session.updated_at,
        )

    # ------------------------------------------------------------------
    # reject
    # ------------------------------------------------------------------

    async def _do_reject(
        self,
        session: ConsultSession,
        session_id: str,
        state: XuanhuState,
        snapshot: dict[str, Any],
        request: ReviewRequest,
        doctor_id: str | None,
        trace_id: str,
    ) -> ReviewResponse:
        """否决处方：回退到 prescription 阶段。"""
        original_formula = self._extract_formula_dict(state)
        sid = uuid.UUID(session_id)

        # 1. 插入 DoctorReview
        review_record = DoctorReview(
            session_id=sid,
            agent_run_id=None,
            safety_rule_run_id=None,
            action="reject",
            original_formula=original_formula,
            formula_override=None,
            feedback=request.feedback,
            reviewed_by=doctor_id,
        )
        self._db.add(review_record)
        await self._db.flush()
        await self._db.refresh(review_record)

        # 2. 构造 doctor_review dict
        doctor_review_dict: dict[str, Any] = {
            "action": "reject",
            "reviewed_by": doctor_id,
            "reviewed_at": _now().isoformat(),
            "review_id": str(review_record.id),
            "feedback": request.feedback,
        }

        # 3. 更新 state_snapshot：回退到 prescription
        new_snapshot = dict(snapshot)
        new_snapshot["doctor_review"] = doctor_review_dict
        new_snapshot["current_stage"] = "prescription"
        new_snapshot["pending_review"] = False
        # 清除安全审核结果（将在下次推进时重新生成）
        # 保留 modified_formula 和 base_formula 供医师参考

        session.current_stage = "prescription"
        session.status = "active"
        session.pending_review = False
        session.state_version += 1
        session.state_snapshot = new_snapshot

        # 4. 写审计
        self._db.add(
            _audit_event(
                session_id=sid,
                event_type="doctor.reviewed",
                actor_type="doctor" if doctor_id else "system",
                actor_id=doctor_id,
                payload={
                    "action": "reject",
                    "review_id": str(review_record.id),
                    "feedback": request.feedback,
                    "state_version": session.state_version,
                    "from_stage": "review",
                    "to_stage": "prescription",
                    "trace_id": trace_id,
                },
                trace_id=trace_id,
            )
        )

        await self._db.flush()
        await self._db.refresh(session)

        await self._try_emit_stream_event(
            session_id=str(session.id),
            event_type="doctor.reviewed",
            payload={
                "action": "reject",
                "review_id": str(review_record.id),
                "to_stage": "prescription",
                "state_version": session.state_version,
            },
        )

        return ReviewResponse(
            session_id=str(session.id),
            action="reject",
            current_stage=session.current_stage,
            status=session.status,
            pending_review=session.pending_review,
            review_id=str(review_record.id),
            state_version=session.state_version,
            feedback=request.feedback,
            updated_at=session.updated_at,
        )

    # ------------------------------------------------------------------
    # 辅助方法
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

    def _build_state_from_snapshot(
        self, snapshot: dict[str, Any], session_id: str
    ) -> XuanhuState:
        """从 PG state_snapshot 重建 XuanhuState（最小化）。"""
        data: dict[str, Any] = {
            "session_id": session_id,
            "current_stage": snapshot.get("current_stage", "review"),
            "pending_review": snapshot.get("pending_review", False),
            "rollback_counts": snapshot.get("rollback_counts", {}),
            "state_version": snapshot.get("state_version", 1),
        }

        # 恢复患者信息
        if "patient_info" in snapshot:
            data["patient_info"] = snapshot["patient_info"]

        # 恢复业务字段
        for key in (
            "safety_rule_result",
            "safety_review",
            "modified_formula",
            "base_formula",
            "doctor_review",
        ):
            if key in snapshot:
                data[key] = snapshot[key]

        return XuanhuState.model_validate(data)

    def _extract_formula_dict(self, state: XuanhuState) -> dict[str, Any] | None:
        """提取当前处方（优先 modified_formula，回退 base_formula）。"""
        if state.modified_formula is not None:
            return state.modified_formula.model_dump(mode="python")
        if state.base_formula is not None:
            return state.base_formula.model_dump(mode="python")
        return None

    async def _get_latest_safety_rule_run_id(
        self, session_id: uuid.UUID
    ) -> uuid.UUID | None:
        """从 DB 查询最新 safety_rule_run 的 id。

        SafetyRuleEngine.check() 已 flush 该记录，直接查询即可。
        """
        from app.models.safety import SafetyRuleRun

        result = await self._db.execute(
            select(SafetyRuleRun.id)
            .where(SafetyRuleRun.session_id == session_id)
            .order_by(SafetyRuleRun.created_at.desc())
            .limit(1)
        )
        row = result.scalar_one_or_none()
        return row

    def _rule_result_to_safety_review(
        self,
        result: Any,  # SafetyRuleResult
    ) -> SafetyReview:
        """从 SafetyRuleResult 生成 SafetyReview。

        复用 Supervisor 的同名方法逻辑，保持一致性。
        """
        if result.passed:
            return SafetyReview(
                passed=True,
                issues=[],
                rollback_target=RollbackTarget.NONE,
                summary="安全规则审核通过，无阻断性问题。",
            )

        blocker_high = [
            i for i in result.issues
            if i.severity in (Severity.BLOCKER, Severity.HIGH)
        ]
        return SafetyReview(
            passed=False,
            issues=result.issues,
            rollback_target=RollbackTarget.MODIFICATION,
            summary=(
                f"安全规则审核未通过，发现 {len(blocker_high)} 个阻断性问题"
                f"（blocker/high）。"
            ),
        )

    async def _try_emit_stream_event(
        self,
        session_id: str,
        event_type: str,
        payload: dict[str, Any],
    ) -> None:
        """Best-effort 写入 Redis Stream 事件。"""
        try:
            event_service = EventService()
            await event_service.append_session_event(
                session_id=session_id,
                event_type=event_type,  # type: ignore[arg-type]
                payload=payload,
            )
        except Exception:
            logger.warning(
                "doctor.reviewed 事件写入 Redis Stream 失败（best-effort）"
                " session_id=%s",
                session_id,
                exc_info=True,
            )
