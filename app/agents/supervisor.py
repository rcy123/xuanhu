"""Supervisor 状态机骨架。

职责：
- 维护 current_stage，按状态机路由到对应 Agent
- 处理回退（完备性不足→问诊；审核失败→开方/加减）
- 在 review 阶段挂起，等待医师确认
- 异常兜底（Agent 超时、schema 校验失败重试上限）
- checkpoint 写入（PG + Redis）
- 审计与 Redis Stream 事件

不产出医学结论，仅做流程控制。
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel
from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.errors import AgentRunError
from app.agents.registry import AgentRegistry
from app.core.config import get_settings
from app.core.exceptions import (
    InvalidStageTransitionError,
    InvalidStateVersionError,
    SessionNotFoundError,
)
from app.core.redis import get_redis
from app.models.audit import AuditEvent
from app.models.consult import ConsultSession
from app.safety.rule_version import SAFETY_RULE_VERSION
from app.schemas.agent import (
    FormulaResult,
    HerbDose,
    InquiryAgentOutput,
    SafetyIssue,
    SafetyReview,
    SafetyRuleResult,
    SufficiencyReport,
    XuanhuState,
)
from app.schemas.types import (
    RecoveryStatus,
    RollbackTarget,
    SafetyIssueType,
    Severity,
    Stage,
)
from app.services.events import EventService
from app.services.session_lock import SessionLock

logger = logging.getLogger("xuanhu.supervisor")

# Redis checkpoint key 前缀（与数据库设计文档 §8.3 / P3-4 一致）
_CHECKPOINT_KEY_PREFIX = "xuanhu:checkpoint:"


class SupervisorResult(BaseModel):
    """Supervisor 推进结果。"""

    state: XuanhuState
    from_stage: Stage
    to_stage: Stage
    agent_name: str | None = None
    trace_id: str = ""
    blocked_reason: str | None = None


def _default_registry() -> AgentRegistry:
    """构造包含 P5-1/2/3 InquiryAgent/SufficiencyAgent/SyndromeAgent 与
    P6-1 PrescriptionAgent、P6-2 ModificationAgent 的默认 Agent 注册表。"""
    from app.agents.inquiry import InquiryAgent
    from app.agents.modification import ModificationAgent
    from app.agents.prescription import PrescriptionAgent
    from app.agents.sufficiency import SufficiencyAgent
    from app.agents.syndrome import SyndromeAgent

    registry = AgentRegistry()
    registry.register(Stage.INQUIRY, InquiryAgent())  # type: ignore[arg-type]  # output_schema 协变安全
    registry.register(Stage.SUFFICIENCY, SufficiencyAgent())  # type: ignore[arg-type]
    registry.register(Stage.SYNDROME, SyndromeAgent())  # type: ignore[arg-type]
    registry.register(Stage.PRESCRIPTION, PrescriptionAgent())  # type: ignore[arg-type]
    registry.register(Stage.MODIFICATION, ModificationAgent())  # type: ignore[arg-type]
    return registry


class Supervisor:
    """Supervisor 状态机。"""

    def __init__(
        self,
        db: AsyncSession,
        *,
        registry: AgentRegistry | None = None,
        event_service: EventService | None = None,
        redis: Redis | None = None,
    ) -> None:
        self._db = db
        self._registry = registry or _default_registry()
        self._event_service = event_service
        self._redis = redis

    # ------------------------------------------------------------------
    # 公开入口
    # ------------------------------------------------------------------

    async def advance(
        self,
        session_id: str,
        trace_id: str,
        *,
        expected_state_version: int | None = None,
        force: bool = False,
    ) -> SupervisorResult:
        """推进会话到下一个阶段。

        流程：
        1. 获取会话锁
        2. 校验会话存在与 state_version
        3. 根据当前阶段路由并调用 Agent
        4. 更新 XuanhuState、consult_sessions
        5. 写入 PG checkpoint（state_snapshot 等）
        6. 写入 Redis checkpoint（best-effort）
        7. 写入 audit_events
        8. 写入 Redis Stream 事件（best-effort）
        9. 释放锁
        """
        lock = SessionLock(self._db, session_id, trace_id)
        await lock.acquire()
        try:
            return await self._advance_locked(
                session_id, trace_id, expected_state_version=expected_state_version, force=force,
            )
        finally:
            await lock.release()

    # ------------------------------------------------------------------
    # 内部实现
    # ------------------------------------------------------------------

    async def _advance_locked(
        self,
        session_id: str,
        trace_id: str,
        *,
        expected_state_version: int | None = None,
        force: bool = False,
    ) -> SupervisorResult:
        # 1. 加载会话
        session = await self._load_session(session_id)

        # 2. state_version 校验
        if expected_state_version is not None and expected_state_version != session.state_version:
            raise InvalidStateVersionError(
                detail=(
                    f"session_id={session_id} expected_state_version={expected_state_version} "
                    f"but current={session.state_version}"
                ),
                retryable=True,
            )

        # 3. 从 PG snapshot 重建 XuanhuState（或最小化新建）
        state = self._build_state_from_session(session, trace_id)
        from_stage = state.current_stage
        if isinstance(from_stage, str):
            from_stage = Stage(from_stage)
        self._ensure_stage_can_advance(session, from_stage)

        # 4. 阶段路由
        try:
            to_stage, agent_name, blocked_reason, state = await self._route_and_run(
                session, state, trace_id, force=force
            )
        except AgentRunError as exc:
            # Agent 失败 → blocked
            return await self._enter_blocked(
                session,
                state,
                from_stage=from_stage,
                blocked_reason=f"agent_failed/{exc.code}",
                trace_id=trace_id,
            )

        if blocked_reason is not None:
            # 已被路由层判定为 blocked（如 rollback 超限）
            return await self._enter_blocked(
                session,
                state,
                from_stage=from_stage,
                blocked_reason=blocked_reason,
                trace_id=trace_id,
            )

        # 5. 更新 state 与 session
        state = state.model_copy(update={"current_stage": to_stage})
        session.current_stage = to_stage.value
        session.state_version += 1
        state.state_version = session.state_version

        # review 阶段挂起
        if to_stage == Stage.REVIEW:
            session.pending_review = True
            session.status = "pending_review"
            state.pending_review = True
        else:
            # 非 review 阶段清除 pending_review（如果之前被挂起后恢复）
            if from_stage == Stage.REVIEW and to_stage == Stage.RECORD:
                # P4-3 不自动从 review 进入 record，因此不会走到这里
                pass

        # 6. 更新 PG snapshot
        session.state_snapshot = self._build_snapshot(state)
        session.last_checkpoint_at = datetime.now(UTC).replace(tzinfo=None)

        # 6.1 医师 force 强制推进审计（仅作用于 sufficiency→syndrome，不绕过后续安全审核）
        if (
            force
            and from_stage == Stage.SUFFICIENCY
            and to_stage == Stage.SYNDROME
            and state.sufficiency_report is not None
            and not state.sufficiency_report.sufficient
        ):
            self._db.add(
                self._audit_event(
                    session_id=session.id,
                    event_type="stage.force_advanced",
                    actor_type="doctor",
                    actor_id=None,
                    payload={
                        "from_stage": from_stage.value,
                        "to_stage": to_stage.value,
                        "stage": Stage.SUFFICIENCY.value,
                        "sufficiency_sufficient": False,
                        "missing": state.sufficiency_report.missing,
                        "state_version": session.state_version,
                        "trace_id": trace_id,
                    },
                    trace_id=trace_id,
                )
            )

        # 7. 写入 audit
        self._db.add(
            self._audit_event(
                session_id=session.id,
                event_type="stage.changed",
                actor_type="system",
                actor_id="supervisor",
                payload={
                    "from_stage": from_stage.value,
                    "to_stage": to_stage.value,
                    "state_version": session.state_version,
                    "agent_name": agent_name,
                    "rollback_counts": state.rollback_counts,
                    "trace_id": trace_id,
                },
                trace_id=trace_id,
            )
        )

        # 8. 写入 Redis checkpoint（best-effort）
        checkpoint_ok = await self._write_redis_checkpoint(session_id, state, trace_id)
        if not checkpoint_ok:
            session.recovery_status = RecoveryStatus.RECOVERING.value
            state.recovery_status = RecoveryStatus.RECOVERING
            session.state_snapshot = self._build_snapshot(state)
            self._db.add(
                self._audit_event(
                    session_id=session.id,
                    event_type="session.recovered",
                    actor_type="system",
                    actor_id="supervisor",
                    payload={
                        "reason": "checkpoint_redis_failed",
                        "state_version": session.state_version,
                        "trace_id": trace_id,
                    },
                    trace_id=trace_id,
                )
            )

        # 9. Redis Stream 事件（best-effort）
        if to_stage == Stage.REVIEW:
            await self._emit_stream_event(
                session_id,
                "review.required",
                {
                    "from_stage": from_stage.value,
                    "to_stage": to_stage.value,
                    "state_version": session.state_version,
                    "trace_id": trace_id,
                    "modified_formula": (
                        state.modified_formula.model_dump(mode="json")
                        if state.modified_formula is not None
                        else {}
                    ),
                    "safety_review": (
                        state.safety_review.model_dump(mode="json")
                        if state.safety_review is not None
                        else {}
                    ),
                },
            )
        else:
            await self._emit_stream_event(
                session_id,
                "stage.changed",
                {
                    "from_stage": from_stage.value,
                    "to_stage": to_stage.value,
                    "state_version": session.state_version,
                    "trace_id": trace_id,
                },
            )

        await self._db.commit()

        return SupervisorResult(
            state=state,
            from_stage=from_stage,
            to_stage=to_stage,
            agent_name=agent_name,
            trace_id=trace_id,
        )

    # ------------------------------------------------------------------
    # 路由核心
    # ------------------------------------------------------------------

    def _ensure_stage_can_advance(self, session: ConsultSession, stage: Stage) -> None:
        """Reject stages that must not be advanced automatically."""
        if stage == Stage.REVIEW:
            raise InvalidStageTransitionError(
                message="会话等待医师确认，不能自动推进",
                detail=(
                    f"session_id={session.id} current_stage=review "
                    f"status={session.status} pending_review={session.pending_review}"
                ),
                retryable=False,
            )
        if stage == Stage.DONE:
            raise InvalidStageTransitionError(
                message="会话已完成，不能继续推进",
                detail=f"session_id={session.id} current_stage=done status={session.status}",
                retryable=False,
            )
        if stage == Stage.BLOCKED:
            raise InvalidStageTransitionError(
                message="会话已阻塞，需先恢复后再推进",
                detail=(
                    f"session_id={session.id} current_stage=blocked "
                    f"status={session.status} blocked_reason={session.blocked_reason}"
                ),
                retryable=False,
            )

    async def _route_and_run(
        self,
        session: ConsultSession,
        state: XuanhuState,
        trace_id: str,
        *,
        force: bool = False,
    ) -> tuple[Stage, str | None, str | None, XuanhuState]:
        """根据当前阶段路由并执行 Agent。

        Returns:
            (to_stage, agent_name, blocked_reason, updated_state)
            blocked_reason 非空时表示需要进入 blocked。
        """
        current = state.current_stage
        if isinstance(current, str):
            current = Stage(current)

        # P6-3: SAFETY 阶段由确定性规则引擎处理，不使用 AgentRegistry
        if current == Stage.SAFETY:
            return await self._run_safety_rule_engine(session, state, trace_id)

        # 1. 检查 Agent 是否注册
        agent = self._registry.get(current)
        if agent is None:
            # 缺少 Agent → blocked
            return Stage.BLOCKED, None, f"missing_agent_for_stage/{current.value}", state

        # 2. 调用 Agent
        result = await agent.run(state, trace_id)
        agent_name = agent.name

        # 3. 将 Agent 输出写入 State 对应字段
        state = self._apply_agent_output(state, current, result.output, evidences=result.evidences)

        # 4. 根据阶段决定下一步
        to_stage, blocked_reason = self._decide_next_stage(state, current, force=force)

        # 5. 如果回退，更新 rollback_counts
        current_stage_val = current.value if isinstance(current, Stage) else current
        to_stage_val = to_stage.value if isinstance(to_stage, Stage) else to_stage
        if blocked_reason is None and to_stage_val in _ROLLBACK_TARGETS.get(current_stage_val, set()):
            state.rollback_counts[current_stage_val] = state.rollback_counts.get(current_stage_val, 0) + 1

        return to_stage, agent_name, blocked_reason, state

    # ------------------------------------------------------------------
    # SAFETY 阶段：确定性规则引擎（P6-3）
    # ------------------------------------------------------------------

    async def _run_safety_rule_engine(
        self,
        session: ConsultSession,
        state: XuanhuState,
        trace_id: str,
    ) -> tuple[Stage, str | None, str | None, XuanhuState]:
        """P6-3: 运行确定性安全规则引擎，不调用 Safety Agent。

        1. 确定审核目标处方：优先 modified_formula，回退 base_formula（记 warning），
           两者皆缺 → blocked。
        2. 调用 SafetyRuleEngine.check() 得 SafetyRuleResult。
        3. 写入 state.safety_rule_result。
        4. 从规则结果自动生成 SafetyReview（兼容既有 _decide_next_stage 路由）。
        5. 路由决策。
        """
        from app.safety.engine import SafetyRuleEngine

        agent_name = "safety_rule_engine"
        pre_warnings: list[str] = []

        # 1. 确定审核目标处方
        if state.modified_formula is not None:
            formula = state.modified_formula.formula
        elif state.base_formula is not None:
            formula = state.base_formula
            pre_warnings.append("加减方未生成，使用基础方进行安全审核。")
        else:
            # 两方皆缺 → blocked
            placeholder = FormulaResult(
                name="missing",
                composition=[HerbDose(herb="missing", dose=None, unit="g")],
                rationale="基础方与加减方均缺失，无法执行安全审核。",
            )
            blocked_result = SafetyRuleResult(
                passed=False,
                issues=[
                    SafetyIssue(
                        type=SafetyIssueType.CAUTION,
                        severity=Severity.BLOCKER,
                        herbs=[],
                        rule_source="SafetyRuleEngine",
                        suggestion="基础方与加减方均缺失，无法执行安全审核。",
                    )
                ],
                normalized_formula=placeholder,
                warnings=list(pre_warnings),
                rule_version=SAFETY_RULE_VERSION,
                execution_order=["formula_missing"],
            )
            state = state.model_copy(
                update={
                    "safety_rule_result": blocked_result,
                    "safety_review": self._rule_result_to_safety_review(blocked_result),
                    "blocked_reason": "safety_formula_missing",
                }
            )
            # 仍尝试写入 safety_rule_runs（保留阻断留痕）
            try:
                engine = SafetyRuleEngine(self._db)
                await engine.persist_result(
                    session_id=str(session.id),
                    trace_id=trace_id,
                    result=blocked_result,
                    formula=placeholder,
                    patient_info=state.patient_info,
                    formula_source="agent_output",
                    agent_run_id=None,
                )
            except Exception:
                logger.warning(
                    "safety_rule_runs 写入失败（formula_missing 路径）"
                    " session_id=%s trace_id=%s",
                    session.id,
                    trace_id,
                    exc_info=True,
                )
            return Stage.BLOCKED, agent_name, "safety_formula_missing", state

        # 2. 运行规则引擎
        engine = SafetyRuleEngine(self._db)
        rule_result = await engine.check(
            formula=formula,
            patient_info=state.patient_info,
            session_id=str(session.id),
            trace_id=trace_id,
            formula_source="agent_output",
        )

        # 3. 合并 warnings
        if pre_warnings:
            rule_result = rule_result.model_copy(
                update={"warnings": list(rule_result.warnings) + pre_warnings}
            )

        # 4. 写入 state
        safety_review = self._rule_result_to_safety_review(rule_result)
        state = state.model_copy(
            update={
                "safety_rule_result": rule_result,
                "safety_review": safety_review,
            }
        )

        # 5. 路由决策
        to_stage, blocked_reason = self._decide_next_stage(state, Stage.SAFETY)

        # 6. rollback 计数
        to_stage_val = to_stage.value if isinstance(to_stage, Stage) else to_stage
        if blocked_reason is None and to_stage_val in _ROLLBACK_TARGETS.get("safety", set()):
            state.rollback_counts["safety"] = state.rollback_counts.get("safety", 0) + 1

        return to_stage, agent_name, blocked_reason, state

    def _rule_result_to_safety_review(
        self,
        result: SafetyRuleResult,
    ) -> SafetyReview:
        """从 SafetyRuleResult 生成 SafetyReview。

        P6-3 不调用 Safety Agent，SafetyReview 直接由规则结果生成。
        P6-4 将在此处插入 SafetyAgent（LLM 解释），但不得覆盖规则的
        ``passed`` / ``issues``。
        """
        if result.passed:
            return SafetyReview(
                passed=True,
                issues=[],
                rollback_target=RollbackTarget.NONE,
                summary="安全规则审核通过，无阻断性问题。",
            )

        # 未通过：存在 blocker/high。
        # 默认回退到 modification；若 state 中无 modified_formula（即审核的是
        # base_formula），则回退到 prescription。此判断在调用方更准确，
        # 这里采用保守默认 modification。
        blocker_high = [
            i for i in result.issues if i.severity in (Severity.BLOCKER, Severity.HIGH)
        ]
        summary = (
            f"安全规则审核未通过，发现 {len(blocker_high)} 个阻断性问题"
            f"（blocker/high）。"
        )
        return SafetyReview(
            passed=False,
            issues=result.issues,
            rollback_target=RollbackTarget.MODIFICATION,
            summary=summary,
        )

    # ------------------------------------------------------------------
    # 阶段决策
    # ------------------------------------------------------------------

    def _decide_next_stage(
        self,
        state: XuanhuState,
        current: Stage,
        *,
        force: bool = False,
    ) -> tuple[Stage, str | None]:
        """根据当前阶段和 State 内容决定下一阶段。"""
        settings = get_settings()
        limit = settings.safety_rollback_limit

        if current == Stage.INQUIRY:
            return Stage.SUFFICIENCY, None

        if current == Stage.SUFFICIENCY:
            report = state.sufficiency_report
            if report is None:
                # 无报告 → blocked（不应发生）
                return Stage.BLOCKED, "sufficiency_report_missing"
            if report.sufficient:
                return Stage.SYNDROME, None
            # 不足且医师 force → 强制推进 syndrome（审计在 advance 层写入）
            if force:
                return Stage.SYNDROME, None
            # 不足 → 回退 inquiry（不计入 rollback_counts）
            return Stage.INQUIRY, None

        if current == Stage.SYNDROME:
            return Stage.PRESCRIPTION, None

        if current == Stage.PRESCRIPTION:
            return Stage.MODIFICATION, None

        if current == Stage.MODIFICATION:
            return Stage.SAFETY, None

        if current == Stage.SAFETY:
            review = state.safety_review
            if review is None:
                return Stage.BLOCKED, "safety_review_missing"
            if review.passed:
                return Stage.REVIEW, None
            # 未通过 → 按 rollback_target 回退
            target = review.rollback_target
            if target == RollbackTarget.PRESCRIPTION:
                count = state.rollback_counts.get("safety", 0) + 1
                if count > limit:
                    return Stage.BLOCKED, "rollback_limit_exceeded"
                return Stage.PRESCRIPTION, None
            if target == RollbackTarget.MODIFICATION:
                count = state.rollback_counts.get("safety", 0) + 1
                if count > limit:
                    return Stage.BLOCKED, "rollback_limit_exceeded"
                return Stage.MODIFICATION, None
            # rollback_target=none 但 passed=false → blocked
            return Stage.BLOCKED, "safety_failed_no_rollback_target"

        if current == Stage.REVIEW:
            # P4-3 review 必须挂起，不得自动进入 record
            # 此处不应被调用（advance 不应在 review 阶段继续）
            return Stage.BLOCKED, "review_auto_advance_not_allowed"

        if current == Stage.RECORD:
            return Stage.DONE, None

        # blocked / done 等终态
        return Stage.BLOCKED, "terminal_stage_cannot_advance"

    # ------------------------------------------------------------------
    # Agent 输出应用到 State
    # ------------------------------------------------------------------

    def _apply_agent_output(
        self,
        state: XuanhuState,
        stage: Stage,
        output: BaseModel,
        *,
        evidences: list[Any] | None = None,
    ) -> XuanhuState:
        """将 Agent 输出写入 State 对应字段。"""
        updates: dict[str, Any] = {}
        if stage == Stage.INQUIRY:
            if isinstance(output, InquiryAgentOutput):
                from app.agents.inquiry import merge_inquiry_output_to_state
                updates = merge_inquiry_output_to_state(state, output)
        elif stage == Stage.SUFFICIENCY:
            if isinstance(output, SufficiencyReport):
                from app.agents.sufficiency import merge_sufficiency_report_to_state

                updates = merge_sufficiency_report_to_state(state, output)
        elif stage == Stage.SYNDROME:
            from app.schemas.agent import SyndromeResult

            if isinstance(output, SyndromeResult):
                from app.agents.syndrome import merge_syndrome_result_to_state

                updates = merge_syndrome_result_to_state(state, output, evidences=evidences)
        elif stage == Stage.PRESCRIPTION:
            from app.schemas.agent import FormulaResult

            if isinstance(output, FormulaResult):
                from app.agents.prescription import merge_formula_result_to_state

                updates = merge_formula_result_to_state(
                    state, output, evidences=evidences
                )
        elif stage == Stage.MODIFICATION:
            from app.schemas.agent import ModifiedFormulaResult

            if isinstance(output, ModifiedFormulaResult):
                from app.agents.modification import (
                    merge_modified_formula_result_to_state,
                )

                updates = merge_modified_formula_result_to_state(
                    state, output, evidences=evidences
                )
        elif stage == Stage.SAFETY:
            if isinstance(output, SafetyReview):
                updates["safety_review"] = output
        elif stage == Stage.RECORD:
            from app.schemas.agent import MedicalRecord

            if isinstance(output, MedicalRecord):
                updates["medical_record"] = output

        if updates:
            return state.model_copy(update=updates)
        return state

    # ------------------------------------------------------------------
    # blocked 处理
    # ------------------------------------------------------------------

    async def _enter_blocked(
        self,
        session: ConsultSession,
        state: XuanhuState,
        *,
        from_stage: Stage,
        blocked_reason: str,
        trace_id: str,
    ) -> SupervisorResult:
        """进入 blocked 状态。"""
        session.status = "blocked"
        session.current_stage = Stage.BLOCKED.value
        session.blocked_reason = blocked_reason
        session.blocked_at = datetime.now(UTC).replace(tzinfo=None)
        session.recovery_status = RecoveryStatus.MANUAL_REQUIRED.value
        session.state_version += 1
        session.last_checkpoint_at = datetime.now(UTC).replace(tzinfo=None)

        state = state.model_copy(
            update={
                "current_stage": Stage.BLOCKED,
                "blocked_reason": blocked_reason,
                "recovery_status": RecoveryStatus.MANUAL_REQUIRED,
                "state_version": session.state_version,
            }
        )
        session.state_snapshot = self._build_snapshot(state)

        # audit
        self._db.add(
            self._audit_event(
                session_id=session.id,
                event_type="session.blocked",
                actor_type="system",
                actor_id="supervisor",
                payload={
                    "from_stage": from_stage.value,
                    "to_stage": Stage.BLOCKED.value,
                    "blocked_reason": blocked_reason,
                    "state_version": session.state_version,
                    "trace_id": trace_id,
                },
                trace_id=trace_id,
            )
        )

        # Redis checkpoint best-effort
        checkpoint_ok = await self._write_redis_checkpoint(str(session.id), state, trace_id)
        if not checkpoint_ok:
            self._db.add(
                self._audit_event(
                    session_id=session.id,
                    event_type="session.recovered",
                    actor_type="system",
                    actor_id="supervisor",
                    payload={
                        "reason": "checkpoint_redis_failed",
                        "state_version": session.state_version,
                        "blocked_reason": blocked_reason,
                        "trace_id": trace_id,
                    },
                    trace_id=trace_id,
                )
            )

        # Redis Stream
        await self._emit_stream_event(
            str(session.id),
            "session.blocked",
            {
                "from_stage": from_stage.value,
                "blocked_reason": blocked_reason,
                "state_version": session.state_version,
                "trace_id": trace_id,
            },
        )

        await self._db.commit()

        return SupervisorResult(
            state=state,
            from_stage=from_stage,
            to_stage=Stage.BLOCKED,
            agent_name=None,
            trace_id=trace_id,
            blocked_reason=blocked_reason,
        )

    # ------------------------------------------------------------------
    # 辅助方法
    # ------------------------------------------------------------------

    async def _load_session(self, session_id: str) -> ConsultSession:
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

    def _build_state_from_session(self, session: ConsultSession, trace_id: str) -> XuanhuState:
        """从 consult_sessions 重建 XuanhuState。"""
        snapshot = session.state_snapshot or {}
        # 优先使用 snapshot 中的字段，缺失时使用 session 直接字段
        current_stage = snapshot.get("current_stage", session.current_stage)
        if isinstance(current_stage, str):
            current_stage = Stage(current_stage)
        recovery_status = snapshot.get("recovery_status", session.recovery_status or "normal")
        if isinstance(recovery_status, str):
            recovery_status = RecoveryStatus(recovery_status)
        data: dict[str, Any] = {
            "session_id": str(session.id),
            "current_stage": current_stage,
            "pending_review": snapshot.get("pending_review", session.pending_review),
            "rollback_counts": snapshot.get("rollback_counts", dict(session.rollback_counts or {})),
            "blocked_reason": snapshot.get("blocked_reason", session.blocked_reason),
            "state_version": session.state_version,
            "recovery_status": recovery_status,
            "trace_id": trace_id,
        }

        # 从 snapshot 恢复业务字段
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

    def _build_snapshot(self, state: XuanhuState) -> dict[str, Any]:
        """构建 PG state_snapshot。"""
        snapshot = state.model_dump(mode="python")
        # 只保留关键字段（与数据库设计文档 §5.1 一致）
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
        return {k: v for k, v in snapshot.items() if k in keys and v is not None}

    async def _write_redis_checkpoint(
        self, session_id: str, state: XuanhuState, trace_id: str
    ) -> bool:
        """写入 Redis checkpoint，返回是否成功。"""
        try:
            redis = self._redis or await get_redis()
            key = f"{_CHECKPOINT_KEY_PREFIX}{session_id}"
            current_stage_value = (
                state.current_stage.value
                if isinstance(state.current_stage, Stage)
                else str(state.current_stage)
            )
            recovery_status_value = (
                state.recovery_status.value
                if isinstance(state.recovery_status, RecoveryStatus)
                else str(state.recovery_status)
            )
            payload = {
                "session_id": session_id,
                "current_stage": current_stage_value,
                "state_version": state.state_version,
                "pending_review": state.pending_review,
                "rollback_counts": state.rollback_counts,
                "blocked_reason": state.blocked_reason,
                "recovery_status": recovery_status_value,
                "trace_id": trace_id,
                "snapshot": self._build_snapshot(state),
                "checkpoint_at": datetime.now(UTC).isoformat(),
            }
            await redis.set(key, json.dumps(payload, ensure_ascii=False), ex=604800)
            return True
        except Exception:
            logger.warning(
                "Redis checkpoint 写入失败 session_id=%s trace_id=%s",
                session_id,
                trace_id,
                exc_info=True,
            )
            return False

    async def _emit_stream_event(
        self,
        session_id: str,
        event_type: str,
        payload: dict[str, Any],
    ) -> None:
        """best-effort 写入 Redis Stream 事件。"""
        try:
            service = self._event_service or EventService()
            await service.append_session_event(
                session_id=session_id,
                event_type=event_type,  # type: ignore[arg-type]
                payload=payload,
            )
        except Exception:
            logger.warning(
                "Redis Stream 事件写入失败 session_id=%s event_type=%s",
                session_id,
                event_type,
                exc_info=True,
            )

    def _audit_event(
        self,
        session_id: uuid.UUID,
        event_type: str,
        actor_type: str,
        actor_id: str | None,
        payload: dict[str, Any],
        trace_id: str,
    ) -> AuditEvent:
        return AuditEvent(
            session_id=session_id,
            event_type=event_type,
            actor_type=actor_type,
            actor_id=actor_id,
            payload=payload,
            trace_id=trace_id,
        )


# 阶段可回退的目标映射（用于判断某阶段变化是否属于回退）
_ROLLBACK_TARGETS: dict[str, set[str]] = {
    "safety": {"prescription", "modification"},
}
