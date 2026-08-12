"""问诊消息回退（rollback to message checkpoint）。

仅允许在 **inquiry** 阶段使用：医师回退到某条消息时，该消息及其之后的
所有消息被删除，同时按追加式事实模型重建权威状态：

- ``observations``：删除 source 在截断集内的行，并把被它们 supersede 的
  行恢复为 active（清除 supersedes 引用）——修正链是线性的，被删除行
  的 supersede 目标必然更早，可直接恢复。
- ``safety_fact_assertions``：同样删除 + 恢复被 supersede 的 confirmed
  断言（supersede 只发生在确认流程，被覆盖的旧断言恢复为 confirmed）。
- ``safety_profiles``：被删除断言涉及的字段保守重置为 unknown（重新收集），
  避免 profile 与事实不一致。
- ``consult_messages``：截断集内的消息物理删除。
- ``state_version`` +1（并发控制），``state_snapshot.last_message`` 指向
  回退后保留的最后一条消息，并插入一条 system 提示消息告知回退完成。

本模块不执行模型调用、不触发 LangGraph、不生成新问题——回退后用户继续
输入一条消息，intake 流程会按新事实自动重算 gates 并生成下一个问题。
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

#: LangGraph checkpoint 持久化表（无 ORM 模型，仅用轻量 Table 定义做回退清理）
from sqlalchemy import Column, MetaData, String, Table, select
from sqlalchemy import delete as sa_delete
from sqlalchemy import text as sa_text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import (
    InvalidStageTransitionError,
    SessionNotFoundError,
    ValidationError,
)
from app.models.audit import AuditEvent
from app.models.consult import ConsultMessage, ConsultSession
from app.models.domain import (
    GateResult,
    GraphRun,
    Observation,
    SafetyFactAssertion,
    SafetyProfile,
)
from app.models.question_contract import QuestionContractRecord, QuestionCoverageEventRecord
from app.schemas.completeness import CompletenessPolicyInput, CompletenessProgress
from app.schemas.domain import (
    CollectionStatus,
    GateDecision,
    LactationValue,
    ObservationSchema,
    ObservationStatus,
    PregnancyValue,
    SafetyProfileSchema,
)
from app.schemas.message import MessageRollbackData
from app.schemas.triage import TRIAGE_POLICY_VERSION, TriageDisposition, TriageGateDetails, TriageGateResult

_CP_METADATA = MetaData()
CheckpointModel = Table(
    "checkpoints",
    _CP_METADATA,
    Column("thread_id", String),
    Column("checkpoint_ns", String),
    Column("checkpoint_id", String),
    extend_existing=True,
)
CheckpointWriteModel = Table(
    "checkpoint_writes",
    _CP_METADATA,
    Column("thread_id", String),
    extend_existing=True,
)
CheckpointBlobModel = Table(
    "checkpoint_blobs",
    _CP_METADATA,
    Column("thread_id", String),
    extend_existing=True,
)

#: safety_fact_assertions.field_name -> (safety_profiles status 列, value 列)
_SAFETY_PROFILE_FIELDS: dict[str, tuple[str, str]] = {
    "allergy": ("allergy_collection_status", "allergens"),
    "pregnancy": ("pregnancy_collection_status", "pregnancy_value"),
    "lactation": ("lactation_collection_status", "lactation_value"),
    "medications": ("medications_collection_status", "medications"),
    "major_conditions": ("major_conditions_collection_status", "major_conditions"),
    "contraindications": ("contraindications_collection_status", "contraindications"),
}

_ROLLBACK_NOTICE_PREFIX = "已回退问诊记录"


def _safe_ref(prefix: str, value: str) -> str:
    import hashlib
    import re

    safe = re.sub(r"[^A-Za-z0-9._:-]+", "_", value).strip("._:-")
    if safe and len(safe) <= 96:
        return f"{prefix}:{safe}"
    return f"{prefix}:{hashlib.sha256(value.encode('utf-8')).hexdigest()}"


async def _recompute_intake_gates(
    db: AsyncSession,
    *,
    session: ConsultSession,
    trace_id: str,
) -> None:
    """回退后用剩余事实重算 completeness gate，使读模型/前端反映真实完整性。

    背景（REAL-SESSION 4292b0b4）：回退删除了消息与事实，但旧的 completeness
    gate（disposition=ready/stagnated）仍被读模型投影为最新版本 → 前端误判
    "问诊已完整/已停滞"而禁用输入或提示恢复。此处以剩余 active observations
    重算 completeness 并写入一条新 gate（input_state_version=回退后版本），
    triage 保守沿用最近一条 triage gate（红旗状态不因回退被重估放宽）。
    """

    from app.agent_runtime.completeness_policy import (
        completeness_to_gate_result_schema,
        evaluate_completeness_policy,
    )
    from app.agent_runtime.reducer import DomainState
    from app.agent_runtime.triage_policy import (
        to_gate_result_schema as triage_gate_schema,
    )
    from app.core.config import get_settings
    from app.services.langgraph_intake import _completeness_snapshot

    # 1. 剩余 observations → ObservationSchema（含修正链，投影层取链头）
    obs_rows = (await db.scalars(select(Observation).where(Observation.session_id == session.id))).all()
    observation_schemas = tuple(
        ObservationSchema(
            observation_id=row.id,
            session_id=row.session_id,
            fact_key=row.fact_key,
            value=row.value,
            normalized_value=row.normalized_value,
            source_message_id=row.source_message_id,
            status=ObservationStatus(row.status),
            confidence=row.confidence,
            supersedes_observation_id=row.supersedes_observation_id,
            created_at=row.created_at,
        )
        for row in obs_rows
    )

    # 2. safety profile → SafetyProfileSchema
    safety_profile_schema: SafetyProfileSchema | None = None
    profile_row = await db.scalar(select(SafetyProfile).where(SafetyProfile.session_id == session.id))
    if profile_row is not None:
        safety_profile_schema = SafetyProfileSchema(
            session_id=session.id,
            allergy_collection_status=CollectionStatus(profile_row.allergy_collection_status),
            allergens=profile_row.allergens,
            pregnancy_collection_status=CollectionStatus(profile_row.pregnancy_collection_status),
            pregnancy_value=(
                PregnancyValue(profile_row.pregnancy_value) if profile_row.pregnancy_value else None
            ),
            lactation_collection_status=CollectionStatus(profile_row.lactation_collection_status),
            lactation_value=(
                LactationValue(profile_row.lactation_value) if profile_row.lactation_value else None
            ),
            medications_collection_status=CollectionStatus(profile_row.medications_collection_status),
            medications=profile_row.medications,
            major_conditions_collection_status=CollectionStatus(profile_row.major_conditions_collection_status),
            major_conditions=profile_row.major_conditions,
            contraindications_collection_status=CollectionStatus(profile_row.contraindications_collection_status),
            contraindications=profile_row.contraindications,
        )

    # 3. triage 保守沿用最近一次 triage gate（红旗状态不因回退重估放宽）
    latest_triage = await db.scalar(
        select(GateResult)
        .where(
            GateResult.session_id == session.id,
            GateResult.gate_name == "triage",
        )
        .order_by(GateResult.input_state_version.desc(), GateResult.created_at.desc())
        .limit(1)
    )
    triage_details = TriageGateDetails(
        disposition=TriageDisposition.CONTINUE,
        candidate_count=0,
        category_counts=(),
        rule_ids=(),
        rules=(),
        source_message_ids=(),
        risk_level="none",
    )
    if latest_triage is not None and isinstance(latest_triage.details, dict):
        details = latest_triage.details
        try:
            disposition = TriageDisposition(details.get("disposition", TriageDisposition.CONTINUE.value))
            triage_details = TriageGateDetails.model_validate(
                {
                    "disposition": disposition,
                    "candidate_count": details.get("candidate_count", 0),
                    "category_counts": details.get("category_counts", []),
                    "rule_ids": details.get("rule_ids", []),
                    "rules": details.get("rules", []),
                    "source_message_ids": details.get("source_message_ids", []),
                    "risk_level": details.get("risk_level", "none"),
                }
            )
        except Exception:
            triage_details = TriageGateDetails(
                disposition=TriageDisposition.CONTINUE,
                candidate_count=0,
                category_counts=(),
                rule_ids=(),
                rules=(),
                source_message_ids=(),
                risk_level="none",
            )

    triage_gate = TriageGateResult(
        gate_name="triage",
        policy_version=TRIAGE_POLICY_VERSION,
        input_state_version=session.state_version,
        decision=(
            GateDecision.PASSED
            if triage_details.disposition is TriageDisposition.CONTINUE
            else GateDecision.BLOCKED
        ),
        details=triage_details,
    )

    # 4. completeness 重算（剩余事实 + 重置 progress）
    state = DomainState(
        session_id=session.id,
        state_version=session.state_version,
        observations=observation_schemas,
        safety_profile=safety_profile_schema,
    )
    domain_snapshot = _completeness_snapshot(state)
    completeness_result = evaluate_completeness_policy(
        CompletenessPolicyInput(
            input_state_version=session.state_version,
            domain_snapshot=domain_snapshot,
            triage_gate=triage_gate,
            progress=CompletenessProgress(),
            slot_based=get_settings().intake_slot_path_enabled,
        )
    )

    # 5. 写入 graph_run + gate_results（读模型按 input_state_version 选最新组）
    run_id = uuid.uuid4()
    db.add(
        GraphRun(
            id=run_id,
            session_id=session.id,
            graph_version="v1",
            command_id=_safe_ref("rollback", trace_id),
            input_state_version=session.state_version,
            status="completed",
            completed_at=datetime.now(UTC),
        )
    )
    await db.flush()  # 先落 run，保证 gate_results.graph_run_id 外键可解析
    for gate_schema in (triage_gate_schema(triage_gate), completeness_to_gate_result_schema(completeness_result)):
        db.add(
            GateResult(
                id=uuid.uuid4(),
                session_id=session.id,
                graph_run_id=run_id,
                gate_name=gate_schema.gate_name,
                policy_version=gate_schema.policy_version,
                input_state_version=session.state_version,
                decision=gate_schema.decision.value,
                details=gate_schema.details,
            )
        )


async def rollback_messages_to(
    db: AsyncSession,
    *,
    session: ConsultSession,
    target_message_id: uuid.UUID,
    trace_id: str,
    reason: str | None = None,
) -> MessageRollbackData:
    """Delete ``target_message_id`` and all later messages, rebuilding domain state.

    Caller must hold the session lock and be inside a transaction.  Raises
    ``ValidationError`` / ``InvalidStageTransitionError`` / ``SessionNotFoundError``
    on precondition failure — nothing is mutated in those cases.
    """
    if session.current_stage != "inquiry":
        raise InvalidStageTransitionError(
            message="仅问诊阶段支持消息回退",
            detail=(
                f"session_id={session.id} current_stage={session.current_stage} "
                "rollback is only supported during inquiry"
            ),
            retryable=False,
        )

    target = await db.get(ConsultMessage, target_message_id)
    if target is None or target.session_id != session.id:
        raise SessionNotFoundError(
            detail=f"session_id={session.id} target message {target_message_id} not found",
            retryable=False,
        )

    # ---- 1. 截断集：target 及之后（按 (created_at, id) 全序） ----
    rows = (
        await db.scalars(
            select(ConsultMessage)
            .where(ConsultMessage.session_id == session.id)
            .order_by(ConsultMessage.created_at, ConsultMessage.id)
        )
    ).all()
    if len(rows) == 1:
        raise ValidationError(
            message="无可回退的消息",
            detail=f"session_id={session.id} only the target message exists",
            retryable=False,
        )
    target_key = (target.created_at, target.id)
    cutoff = [row for row in rows if (row.created_at, row.id) >= target_key]
    cutoff_ids = {row.id for row in cutoff}
    kept = [row for row in rows if row.id not in cutoff_ids]

    # ---- 2. R9 提问契约：删除引用截断集内消息的 coverage 事件与契约 ----
    # question_coverage_events.answer_message_id / question_contracts.question_message_id
    # 均为 RESTRICT 外键；contract 删除会级联清理其 root/parent 子契约与 coverage。
    deleted_coverage = (
        await db.scalars(
            select(QuestionCoverageEventRecord).where(QuestionCoverageEventRecord.answer_message_id.in_(cutoff_ids))
        )
    ).all()
    for coverage in deleted_coverage:
        await db.delete(coverage)
    deleted_contracts = (
        await db.scalars(
            select(QuestionContractRecord).where(QuestionContractRecord.question_message_id.in_(cutoff_ids))
        )
    ).all()
    for contract in deleted_contracts:
        await db.delete(contract)

    # ---- 3. observations：删除 + 恢复 supersede 链 ----
    deleted_obs = (await db.scalars(select(Observation).where(Observation.source_message_id.in_(cutoff_ids)))).all()
    restore_obs_ids = {
        obs.supersedes_observation_id for obs in deleted_obs if obs.supersedes_observation_id is not None
    }
    for obs in deleted_obs:
        await db.delete(obs)
    if restore_obs_ids:
        restore_obs = (await db.scalars(select(Observation).where(Observation.id.in_(restore_obs_ids)))).all()
        for obs in restore_obs:
            # 被删除行 supersede 的目标必然更早且在同一事实链上；恢复为唯一 current truth。
            obs.status = "active"
            obs.supersedes_observation_id = None

    # ---- 4. safety assertions：删除 + 恢复被 supersede 的 confirmed 断言 ----
    deleted_assertions = (
        await db.scalars(select(SafetyFactAssertion).where(SafetyFactAssertion.source_message_id.in_(cutoff_ids)))
    ).all()
    restore_assertion_ids = {
        assertion.supersedes_assertion_id
        for assertion in deleted_assertions
        if assertion.supersedes_assertion_id is not None
    }
    affected_fields: set[str] = {assertion.field_name for assertion in deleted_assertions}
    for assertion in deleted_assertions:
        await db.delete(assertion)
    if restore_assertion_ids:
        restore_assertions = (
            await db.scalars(select(SafetyFactAssertion).where(SafetyFactAssertion.id.in_(restore_assertion_ids)))
        ).all()
        for assertion in restore_assertions:
            # supersede 只发生在确认流程，被覆盖的旧断言此前必为 confirmed。
            if assertion.status == "superseded":
                assertion.status = "confirmed"
                assertion.supersedes_assertion_id = None
                assertion.superseded_at = None

    # ---- 5. safety profile：受影响字段保守重置为 unknown ----
    if affected_fields:
        profile = await db.scalar(select(SafetyProfile).where(SafetyProfile.session_id == session.id))
        if profile is not None:
            for field in affected_fields:
                mapping = _SAFETY_PROFILE_FIELDS.get(field)
                if mapping is None:
                    continue
                status_col, value_col = mapping
                setattr(profile, status_col, "unknown")
                setattr(profile, value_col, None)

    # ---- 6. 删除截断集内的消息 ----
    for row in cutoff:
        await db.delete(row)
    await db.flush()

    # ---- 7. 会话状态推进 ----
    session.state_version += 1
    snapshot = dict(session.state_snapshot or {})
    snapshot["agent_runtime"] = "langgraph"
    kept_last = kept[-1] if kept else None
    if kept_last is not None:
        snapshot["last_message"] = {
            "message_id": str(kept_last.id),
            "role": kept_last.role,
            "stage": kept_last.stage,
            "preview": kept_last.content[:200],
            "created_at": kept_last.created_at.isoformat() if kept_last.created_at else None,
        }
    else:
        snapshot.pop("last_message", None)
    # 7.1 同步 langgraph_intake 的当前问题指针：回退后 reply binding 依赖
    # last_question_message_id 判定"回复绑定的问题"——若不更新，用户回答保留的
    # 提问（如"大小便情况"）会被 _resolve_reply_binding 判为"非当前问题"而拒绝
    # （REAL-SESSION 4292b0b4：提交全部 HANDLER_REJECTED，消息不落库）。
    intake = dict(snapshot.get("langgraph_intake") or {})
    latest_question = next(
        (
            row
            for row in reversed(kept)
            if row.role == "agent"
            and row.agent_name == "question_composer"
            and not (isinstance(row.structured_delta, dict) and row.structured_delta.get("kind") == "completion_notice")
        ),
        None,
    )
    if latest_question is not None:
        intake["last_question_message_id"] = str(latest_question.id)
    else:
        intake.pop("last_question_message_id", None)
    latest_patient = next(
        (row for row in reversed(kept) if row.role in {"doctor", "patient_proxy"}),
        None,
    )
    if latest_patient is not None:
        intake["last_patient_message_id"] = str(latest_patient.id)
    else:
        intake.pop("last_patient_message_id", None)
    if intake:
        snapshot["langgraph_intake"] = intake
    snapshot["rollback"] = {
        "target_message_id": str(target_message_id),
        "rolled_back_count": len(cutoff),
        "at": datetime.now(UTC).isoformat(),
    }
    session.state_snapshot = snapshot

    # ---- 7.5 重算 completeness gate：回退后读模型/前端反映真实完整性 ----
    # 否则旧 gate（ready/stagnated）仍被投影，前端误判"已完整/已停滞"而禁用输入。
    await _recompute_intake_gates(db, session=session, trace_id=trace_id)

    # ---- 7.6 清理 LangGraph checkpoints：回退后旧 pending interrupt 失效 ----
    # 否则下一条消息提交会 resume 引用已删除消息的 interrupt →
    # RUNNER_EXECUTION_FAILED（REAL-SESSION 4292b0b4：回退后所有提交失败，
    # 消息落库但 agent 不回复）。回退是状态重置，checkpoint 一并清除，
    # 下一条消息走全新 ainvoke。
    thread_id = f"v1:{session.id}"
    # 表由 LangGraph saver.setup() 在 runtime 启动时创建；先检查存在性避免误伤。
    for checkpoint_table in (CheckpointWriteModel, CheckpointBlobModel, CheckpointModel):
        exists = await db.scalar(
            sa_text("SELECT to_regclass(:name)").bindparams(name=f"public.{checkpoint_table.name}")
        )
        if exists is not None:
            await db.execute(sa_delete(checkpoint_table).where(checkpoint_table.c.thread_id == thread_id))

    # ---- 8. system 提示消息 ----
    if reason:
        notice = f"{_ROLLBACK_NOTICE_PREFIX}，共撤销 {len(cutoff)} 条。原因：{reason[:200]}"
    else:
        notice = f"{_ROLLBACK_NOTICE_PREFIX}，共撤销 {len(cutoff)} 条，请继续补充信息。"
    db.add(
        ConsultMessage(
            id=uuid.uuid4(),
            session_id=session.id,
            role="system",
            stage=session.current_stage,
            content=notice,
            trace_id=_safe_ref("trace", trace_id),
        )
    )

    # ---- 9. 审计 ----
    db.add(
        AuditEvent(
            session_id=session.id,
            event_type="messages.rolled_back.v1",
            actor_type="doctor",
            actor_id=None,
            payload={
                "target_message_id": str(target_message_id),
                "rolled_back_message_ids": [str(row.id) for row in cutoff],
                "rolled_back_count": len(cutoff),
                "kept_last_message_id": str(kept_last.id) if kept_last else None,
                "state_version": session.state_version,
                "reason": reason,
            },
            trace_id=_safe_ref("trace", trace_id),
        )
    )

    return MessageRollbackData(
        session_id=str(session.id),
        current_stage=session.current_stage,
        state_version=session.state_version,
        rolled_back_message_ids=[str(row.id) for row in cutoff],
        kept_last_message_id=str(kept_last.id) if kept_last else None,
    )


def rollback_notice_payload() -> dict[str, Any]:
    """Return a stable structured payload for the rollback notice message."""
    return {"kind": "rollback_notice", "version": "message-rollback.v1"}
