"""Product LangGraph Safety and human-review orchestration.

Domain artifacts are authoritative.  ``safety_rule_runs`` and
``doctor_reviews`` remain transactional compatibility projections.  Graph
State and interrupt/resume values carry references only.
"""

from __future__ import annotations

import contextlib
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, NoReturn, cast

from pydantic import BaseModel, ConfigDict, ValidationError
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent_runtime.checkpoint import postgres_checkpointer
from app.agent_runtime.commands import NODE_BLOCKED_TERMINAL, NODE_REVIEW_PLACEHOLDER, XuanhuCommand
from app.agent_runtime.config import DEFAULT_GRAPH_VERSION, make_run_config
from app.agent_runtime.formula_consistency import FORMULA_CONSISTENCY_POLICY_VERSION
from app.agent_runtime.graph import build_main_graph
from app.agent_runtime.observation_projection import project_current_observations
from app.agent_runtime.reducer import DomainDelta, DomainState
from app.agent_runtime.repository import (
    ArtifactPayloadRecord,
    ArtifactPayloadSpec,
    AuditEventSpec,
    DoctorReviewSpec,
    GraphStepSpec,
    PostgresDomainRepository,
    RepositoryError,
    RepositoryErrorCode,
    SafetyRuleRunSpec,
    artifact_payload_digest,
)
from app.agent_runtime.runner import GraphRunner
from app.agent_runtime.specs import AgentSpec, ModelPolicy, RunArtifact, RunSpec
from app.agent_runtime.state import ArtifactRef, XuanhuGraphState, default_state
from app.agent_runtime.verifiers import VerificationContext
from app.core.config import get_settings
from app.core.exceptions import (
    FormulaOverrideRequiredError,
    InvalidStageTransitionError,
    InvalidStateVersionError,
    ModelGatewayUnavailableError,
    SafetyReviewBlockedError,
    SessionNotFoundError,
    SessionTerminatedError,
)
from app.db.session import get_session_factory
from app.models.consult import ConsultSession
from app.models.domain import GateResult, GraphRun, IntakeCommandClaim
from app.models.review import DoctorReview
from app.models.safety import SafetyRuleRun
from app.safety.engine import SafetyRuleEngine
from app.schemas.agent import FormulaResult, HerbDose, SafetyRuleResult
from app.schemas.domain import (
    ArtifactRevisionSchema,
    ArtifactStatus,
    CollectionStatus,
    GateDecision,
    GateResultSchema,
    LactationValue,
    PregnancyValue,
    SafetyProfileSchema,
)
from app.schemas.formula import FormulaDraft, FormulaDraftDecision
from app.schemas.review import FormulaOverride, ReviewRequest, ReviewResponse
from app.schemas.session import PatientInfo
from app.schemas.types import Gender, PregnancyStatus, Severity

FORMULA_ARTIFACT_TYPE = "formula_draft"
REVIEWED_FORMULA_ARTIFACT_TYPE = "reviewed_formula"
SAFETY_ARTIFACT_TYPE = "safety_result"
REVIEWED_FORMULA_ATTEMPT_ARTIFACT_TYPE = "reviewed_formula_attempt"
SAFETY_RECHECK_ATTEMPT_ARTIFACT_TYPE = "safety_recheck_attempt"
REVIEW_SUBMISSION_ARTIFACT_TYPE = "review_submission"
DOCTOR_REVIEW_ARTIFACT_TYPE = "doctor_review"

FORMULA_PAYLOAD_SCHEMA_VERSION = "formula-artifact-payload.v1"
REVIEWED_FORMULA_SCHEMA_VERSION = "reviewed-formula.v1"
SAFETY_PAYLOAD_SCHEMA_VERSION = "safety-result.v1"
REVIEW_SUBMISSION_SCHEMA_VERSION = "review-submission.v1"
DOCTOR_REVIEW_SCHEMA_VERSION = "doctor-review.v1"

SAFETY_POLICY_VERSION = "safety-rule-engine.product.v1"
REVIEW_POLICY_VERSION = "doctor-review-interrupt.product.v1"
PRODUCT_AGENT_SPEC_VERSION = "langgraph-product-domain-delta.v1"
PRODUCT_PROMPT_VERSION = "deterministic-no-prompt.v1"

# 安全硬门禁不通过时自动重开方的最大尝试次数（含首次）。
# 每次安全失败自动重置回 syndrome + 写入拦截原因 + 失效方子 → 模型带原因重开方。
# 超过此次数仍失败才落 blocked（前端「修改处方」兜底）。REAL-SESSION cb5fe635 复盘。
MAX_SAFETY_REOPEN_ATTEMPTS = 3


def _safety_feedback_text(result: SafetyRuleResult) -> str:
    """把安全引擎拦截原因拼成注入重新开方的模型反馈（review_feedback 通道）。"""
    suggestions = [item.suggestion for item in result.issues if item.suggestion]
    if not suggestions:
        return ""
    return "以下安全审核问题必须修正：\n" + "\n".join(f"- {item}" for item in suggestions)


async def _safety_attempt_count(
    repository: PostgresDomainRepository,
    session_id: uuid.UUID,
    formula_revision: int,
) -> int:
    """统计当前方子已接受安全引擎评估的次数（safety_result artifact 版本数）。

    计数按方子 revision 维度（而非全局），保证每次重新开方后自动重开次数重置——
    新方子应获得全新的自动重开预算，不被历史方子的失败次数耗尽。
    """
    safety_id = _stable_id(session_id, SAFETY_ARTIFACT_TYPE)
    try:
        from app.models.domain import ArtifactRevision, ArtifactRevisionPayload

        factory = get_session_factory()
        async with factory() as db:
            rows = (
                await db.execute(
                    select(ArtifactRevision, ArtifactRevisionPayload)
                    .join(
                        ArtifactRevisionPayload,
                        ArtifactRevisionPayload.artifact_revision_id == ArtifactRevision.id,
                    )
                    .where(
                        ArtifactRevision.session_id == session_id,
                        ArtifactRevision.artifact_id == safety_id,
                    )
                    .order_by(ArtifactRevision.revision.desc())
                )
            ).all()
    except Exception:  # noqa: BLE001 - 计数失败保守按 0（自动重开，safety 硬门禁兜底）
        return 0
    count = 0
    for _artifact, payload in rows:
        if payload is None or not isinstance(payload.payload, dict):
            continue
        formula_ref = payload.payload.get("formula_ref")
        if isinstance(formula_ref, dict) and formula_ref.get("revision") == formula_revision:
            count += 1
    return count


class _NoInput(BaseModel):
    model_config = ConfigDict(extra="forbid")


@dataclass(frozen=True, slots=True)
class FormulaAuthority:
    record: ArtifactPayloadRecord
    formula: FormulaResult


@dataclass(frozen=True, slots=True)
class PreparedReview:
    session_id: uuid.UUID
    state_version: int
    formula: FormulaAuthority
    safety_record: ArtifactPayloadRecord
    safety_result: SafetyRuleResult
    safety_rule_run_id: uuid.UUID
    interrupt_payload: dict[str, str]
    from_blocked_safety: bool = False


@dataclass(frozen=True, slots=True)
class _ValidatedSubmission:
    record: ArtifactPayloadRecord
    projection: DoctorReview
    payload: dict[str, Any]
    action: str


class ReviewResumeRejected(Exception):
    """A submitted reference was rejected and the interrupt must stay open."""

    def __init__(self, *, code: str, detail: str) -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail


@dataclass(frozen=True, slots=True)
class _SessionMeta:
    current_stage: str
    status: str
    pending_review: bool
    state_version: int
    agent_runtime: str
    patient_info: dict[str, Any]
    blocked_reason: str | None = None
    state_snapshot: dict[str, Any] | None = None


def _stable_id(session_id: uuid.UUID, kind: str) -> uuid.UUID:
    return uuid.uuid5(uuid.NAMESPACE_URL, f"xuanhu:{kind}:{session_id}")


def _review_artifact_types(*, safety_passed: bool) -> tuple[str, str]:
    if safety_passed:
        return REVIEWED_FORMULA_ARTIFACT_TYPE, SAFETY_ARTIFACT_TYPE
    return REVIEWED_FORMULA_ATTEMPT_ARTIFACT_TYPE, SAFETY_RECHECK_ATTEMPT_ARTIFACT_TYPE


def _bounded_trace(value: str) -> str:
    return value[:64]


def _node_trace_id(state: XuanhuGraphState) -> str:
    value = state.get("run_id", "")
    return _bounded_trace(value if value else "langgraph-review")


def _safe_error(state: XuanhuGraphState, code: str, detail: str) -> dict[str, Any]:
    return {
        "route": NODE_REVIEW_PLACEHOLDER,
        "pending_interrupt": None,
        "last_error": {
            "code": code,
            "trace_id": state.get("run_id", ""),
            "detail": detail,
        },
    }


def _reject_resume(code: str, detail: str) -> NoReturn:
    raise ReviewResumeRejected(code=code, detail=detail)


async def _session_meta(session_id: uuid.UUID) -> _SessionMeta:
    factory = get_session_factory()
    async with factory() as db:
        row = await db.get(ConsultSession, session_id)
        if row is None:
            raise SessionNotFoundError(detail=f"session_id={session_id} not found", retryable=False)
        return _SessionMeta(
            current_stage=row.current_stage,
            status=row.status,
            pending_review=row.pending_review,
            state_version=row.state_version,
            agent_runtime=row.agent_runtime,
            patient_info=dict(row.patient_info or {}),
            blocked_reason=row.blocked_reason,
            state_snapshot=dict(row.state_snapshot or {}),
        )


def _formula_ref(authority: FormulaAuthority) -> dict[str, object]:
    return {
        "artifact_type": authority.record.artifact_type,
        "artifact_id": str(authority.record.artifact_id),
        "revision": authority.record.revision,
        "content_digest": authority.record.content_digest,
    }


def _formula_result_from_draft(record: ArtifactPayloadRecord) -> FormulaResult:
    if record.payload_schema_version != FORMULA_PAYLOAD_SCHEMA_VERSION:
        raise RepositoryError(RepositoryErrorCode.ARTIFACT_PAYLOAD_INVALID)
    payload = record.payload
    if payload.get("kind") != FORMULA_ARTIFACT_TYPE:
        raise RepositoryError(RepositoryErrorCode.ARTIFACT_PAYLOAD_INVALID)
    verification = payload.get("verification")
    consistency = payload.get("consistency")
    if (
        not isinstance(verification, dict)
        or verification.get("passed") is not True
        or not isinstance(consistency, dict)
        or consistency.get("passed") is not True
    ):
        raise RepositoryError(RepositoryErrorCode.ARTIFACT_PAYLOAD_INVALID)
    try:
        draft = FormulaDraft.model_validate(payload.get("output"))
    except ValidationError:
        raise RepositoryError(RepositoryErrorCode.ARTIFACT_PAYLOAD_INVALID) from None
    candidate = draft.candidate_formula
    if (
        draft.decision is not FormulaDraftDecision.COMPLETED
        or draft.review_required is not True
        or candidate is None
    ):
        raise RepositoryError(RepositoryErrorCode.ARTIFACT_PAYLOAD_INVALID)
    return FormulaResult(
        name=candidate.name,
        composition=[
            HerbDose(herb=item.herb, dose=item.dose, unit=item.unit, note=item.note)
            for item in candidate.composition
        ],
        source=None,
        rationale=candidate.rationale,
        citations=[],
    )


def _formula_result_from_review(record: ArtifactPayloadRecord) -> FormulaResult:
    if record.payload_schema_version != REVIEWED_FORMULA_SCHEMA_VERSION:
        raise RepositoryError(RepositoryErrorCode.ARTIFACT_PAYLOAD_INVALID)
    if record.payload.get("kind") != REVIEWED_FORMULA_ARTIFACT_TYPE:
        raise RepositoryError(RepositoryErrorCode.ARTIFACT_PAYLOAD_INVALID)
    try:
        return FormulaResult.model_validate(record.payload.get("formula"))
    except ValidationError:
        raise RepositoryError(RepositoryErrorCode.ARTIFACT_PAYLOAD_INVALID) from None


async def _require_completed_producer(record: ArtifactPayloadRecord) -> None:
    factory = get_session_factory()
    async with factory() as db:
        run = await db.get(GraphRun, record.produced_by_run_id)
        if (
            run is None
            or run.session_id != record.session_id
            or run.input_state_version != record.input_state_version
            or run.status != "completed"
        ):
            raise RepositoryError(RepositoryErrorCode.ARTIFACT_PAYLOAD_INVALID)


async def _require_formula_draft_gates(record: ArtifactPayloadRecord) -> None:
    """Bind a draft to its completed producer and both persisted verifier gates."""

    await _require_completed_producer(record)
    factory = get_session_factory()
    async with factory() as db:
        rows = tuple(
            await db.scalars(
                select(GateResult).where(
                    GateResult.session_id == record.session_id,
                    GateResult.graph_run_id == record.produced_by_run_id,
                    GateResult.input_state_version == record.input_state_version,
                    GateResult.gate_name.in_(("formula_consistency", "canonical_verifier_chain")),
                )
            )
        )
    consistency = [
        row
        for row in rows
        if row.gate_name == "formula_consistency"
        and row.policy_version == FORMULA_CONSISTENCY_POLICY_VERSION
        and row.decision == "passed"
        and isinstance(row.details, dict)
        and row.details.get("artifact_digest") == record.content_digest
    ]
    canonical = [
        row
        for row in rows
        if row.gate_name == "canonical_verifier_chain"
        and row.policy_version == "l2-4-v1"
        and row.decision == "passed"
    ]
    if len(consistency) != 1 or len(canonical) != 1:
        raise RepositoryError(RepositoryErrorCode.ARTIFACT_PAYLOAD_INVALID)


async def _load_formula_authority(
    repository: PostgresDomainRepository,
    session_id: uuid.UUID,
) -> FormulaAuthority:
    reviewed = await repository.get_artifact_payload(
        session_id,
        artifact_type=REVIEWED_FORMULA_ARTIFACT_TYPE,
        artifact_id=_stable_id(session_id, REVIEWED_FORMULA_ARTIFACT_TYPE),
        status="current",
    )
    if reviewed is not None:
        await _require_completed_producer(reviewed)
        return FormulaAuthority(reviewed, _formula_result_from_review(reviewed))
    draft = await repository.get_artifact_payload(
        session_id,
        artifact_type=FORMULA_ARTIFACT_TYPE,
        artifact_id=_stable_id(session_id, FORMULA_ARTIFACT_TYPE),
        status="current",
    )
    if draft is None:
        raise RepositoryError(RepositoryErrorCode.ARTIFACT_PAYLOAD_INVALID)
    await _require_formula_draft_gates(draft)
    return FormulaAuthority(draft, _formula_result_from_draft(draft))


def _observation_value(fact: Any) -> Any:
    """观察事实的有效值：normalized_value 优先，且区分 ``0``/空字符串与缺省。"""
    normalized = getattr(fact, "normalized_value", None)
    if normalized is not None:
        return normalized
    return getattr(fact, "value", None)


def _parse_patient_age(raw: Any) -> int | None:
    """解析 observation 中的年龄，非法/越界一律返回 ``None`` 且不抛异常。

    - bool 一律拒绝（bool 是 int 子类，``int(True) == 1`` 会产生虚假年龄）；
    - 接受 int（含 0）或可转 int 的字符串；
    - 超出 ``PatientInfo.age`` 合法范围 [0, 130] 的年龄忽略。
    """
    if isinstance(raw, bool) or raw is None:
        return None
    if isinstance(raw, int):
        candidate = raw
    elif isinstance(raw, str):
        text = raw.strip()
        if not text:
            return None
        try:
            candidate = int(text)
        except ValueError:
            return None
    else:
        return None
    return candidate if 0 <= candidate <= 130 else None


def _patient_info_from_domain(
    profile: SafetyProfileSchema,
    observations: tuple[Any, ...] = (),
) -> PatientInfo:
    # 2026-08：PatientInfo 补充 domain observations 里的性别/年龄（patient.sex /
    # patient.age）。安全引擎的妊娠/剂量检查依赖它们——缺省时妊娠未知警告在男性
    # 患者上误报（真实会话 f6a5ffb7 复盘）。
    #
    # R4-A：性别/年龄只从 project_current_observations 投影出的**当前语义链头**
    # 读取——被 CORRECTED/RETRACTED 取代的根不是当前真值，输入顺序也不决定赢家。
    # 同一组当前事实在任何历史顺序下都投影出同一 PatientInfo；校正值胜出，撤回值
    # 消失为 UNKNOWN/None。投影顺序稳定（(session_id, fact_key, observation_id)），
    # 同一 fact_key 存在多个当前链头时后者确定性胜出。函数不修改 observations/profile。
    gender = Gender.UNKNOWN
    age: int | None = None
    for item in project_current_observations(observations):
        key = getattr(item, "fact_key", None)
        if key == "patient.sex":
            raw = _observation_value(item)
            if isinstance(raw, str):
                sex_text = raw.strip().lower()
                if sex_text:
                    with contextlib.suppress(ValueError):
                        gender = Gender(sex_text)
        elif key == "patient.age":
            parsed = _parse_patient_age(_observation_value(item))
            if parsed is not None:
                age = parsed
    pregnancy = (
        PregnancyStatus.PREGNANT
        if profile.pregnancy_value is PregnancyValue.PREGNANT
        else PregnancyStatus.POSSIBLE
        if profile.pregnancy_value is PregnancyValue.POSSIBLE
        else PregnancyStatus.NO
        if profile.pregnancy_value is PregnancyValue.NOT_PREGNANT
        # 患者已明确确认「无」（explicitly_none）：value 为 None，但状态本身就是
        # 否定确认，不得降级为 UNKNOWN —— 否则已确认的妊娠否定会被安全引擎
        # 误报「妊娠状态未确认」（真实会话 794ad8e4 复盘）。
        or profile.pregnancy_collection_status is CollectionStatus.EXPLICITLY_NONE
        else PregnancyStatus.UNKNOWN
    )
    lactation = (
        LactationValue.LACTATING
        if profile.lactation_value is LactationValue.LACTATING
        else LactationValue.NOT_LACTATING
        if profile.lactation_value is LactationValue.NOT_LACTATING
        # 同 pregnancy：explicitly_none 视为否定确认。
        or profile.lactation_collection_status is CollectionStatus.EXPLICITLY_NONE
        else None
    )
    return PatientInfo(
        gender=gender,
        age=age,
        allergies=list(profile.allergens or ()),
        pregnancy_status=pregnancy,
        current_medications=list(profile.medications or ()),
        major_conditions=list(profile.major_conditions or ()),
        special_conditions=list(profile.contraindications or ()),
        lactation_status=lactation,
    )


def _safety_preconditions(profile: SafetyProfileSchema | None) -> bool:
    if profile is None:
        return False
    return all(
        status is not CollectionStatus.UNKNOWN
        for status in (
            profile.allergy_collection_status,
            profile.medications_collection_status,
            profile.major_conditions_collection_status,
        )
    )


def _artifact_revision(
    *,
    session_id: uuid.UUID,
    artifact_id: uuid.UUID,
    artifact_type: str,
    state_version: int,
    run_id: uuid.UUID,
    latest: ArtifactPayloadRecord | None,
) -> ArtifactRevisionSchema:
    return ArtifactRevisionSchema(
        artifact_id=artifact_id,
        artifact_type=artifact_type,
        revision=1 if latest is None else latest.revision + 1,
        session_id=session_id,
        input_state_version=state_version,
        status=ArtifactStatus.CURRENT,
        produced_by_run_id=run_id,
        parent_revision_id=None if latest is None else latest.artifact_revision_row_id,
        parent_revision=None if latest is None else latest.revision,
        created_at=datetime.now(UTC),
    )


def _payload_spec(
    artifact: ArtifactRevisionSchema,
    *,
    schema_version: str,
    payload: dict[str, object],
) -> ArtifactPayloadSpec:
    return ArtifactPayloadSpec(
        session_id=artifact.session_id,
        artifact_id=artifact.artifact_id,
        revision=artifact.revision,
        payload_schema_version=schema_version,
        payload=payload,
        content_digest=artifact_payload_digest(schema_version, payload),
    )


def _verification_context(
    delta: DomainDelta,
    state: DomainState,
    *,
    stage: str,
    idempotency_key: str,
    trace_id: str,
    policy_version: str = REVIEW_POLICY_VERSION,
) -> VerificationContext:
    spec = AgentSpec(
        name="langgraph_product_domain_delta",
        version=PRODUCT_AGENT_SPEC_VERSION,
        input_schema=_NoInput,
        output_schema=DomainDelta,
        model_policy=ModelPolicy(model="deterministic-product-boundary", max_attempts=1),
    )
    run_spec = RunSpec(
        run_id=delta.run_id,
        session_id=delta.session_id,
        state_version=delta.expected_state_version,
        stage=stage,
        agent_spec_version=spec.version,
        prompt_version=PRODUCT_PROMPT_VERSION,
        policy_version=policy_version,
        deadline_at=datetime.now(UTC) + timedelta(minutes=5),
        total_attempt_budget=1,
        idempotency_key=idempotency_key,
        trace_id=trace_id,
    )
    artifact = RunArtifact(
        output=delta,
        model_actual=None,
        attempts=1,
        latency_ms=0,
        trace_id=trace_id,
        run_id=delta.run_id,
        agent_spec_version=spec.version,
        prompt_version=PRODUCT_PROMPT_VERSION,
    )
    return VerificationContext(
        agent_spec=spec,
        run_spec=run_spec,
        artifact=artifact,
        state=state,
        allowed_stages=frozenset({stage}),
    )


def _preserved_advance(meta: _SessionMeta) -> dict[str, Any] | None:
    """提取旧 snapshot 中的 advance 出处（intake→syndrome 的 completeness gate）。

    该出处是 syndrome 阶段重新 advance 时 reasoning 预检的依据；review 模块
    的任何阶段转换（safety→review/blocked、review→record/syndrome/inquiry）
    都不改变 intake 来源门，必须原样保留，否则被否决/拦截的方子无法重新开方。
    """
    snapshot = meta.state_snapshot or {}
    advance = snapshot.get("advance")
    return advance if isinstance(advance, dict) else None


def _session_updates(
    *,
    current_stage: str,
    status: str,
    pending_review: bool,
    state_version: int,
    route: str,
    blocked_reason: str | None = None,
    preserve_advance: dict[str, Any] | None = None,
    review_feedback: str | None = None,
) -> dict[str, object]:
    snapshot: dict[str, object] = {
        "agent_runtime": "langgraph",
        "current_stage": current_stage,
        "state_version": state_version,
        "pending_review": pending_review,
        "langgraph_review": {
            "version": REVIEW_POLICY_VERSION,
            "route": route,
        },
    }
    # reject 回到 syndrome 时保留 intake→syndrome 的 advance 出处（completeness
    # gate），否则 syndrome 阶段重新 advance 时 reasoning 预检找不到来源门 →
    # REASONING_PRECHECK_FAILED（被否决的方子无法重新开方）。
    if preserve_advance is not None:
        snapshot["advance"] = preserve_advance
    # 医师回退反馈：重新辨证/开方时注入模型输入。
    if review_feedback:
        snapshot["review_feedback"] = review_feedback
    return {
        "current_stage": current_stage,
        "status": status,
        "pending_review": pending_review,
        "recovery_status": "manual_required" if status == "blocked" else "normal",
        "blocked_reason": blocked_reason,
        "blocked_at": datetime.now(UTC).replace(tzinfo=None) if status == "blocked" else None,
        "state_snapshot": snapshot,
    }


def _gate(
    name: str,
    state_version: int,
    decision: GateDecision,
    details: dict[str, object],
) -> GateResultSchema:
    return GateResultSchema(
        gate_name=name,
        policy_version=SAFETY_POLICY_VERSION if name == "safety_rule_engine" else REVIEW_POLICY_VERSION,
        input_state_version=state_version,
        decision=decision,
        details=details,
    )


async def _complete_advance_claim(
    *,
    session_id: uuid.UUID,
    command_id: str,
    response: dict[str, Any],
    state_version: int,
) -> None:
    factory = get_session_factory()
    async with factory() as db, db.begin():
        claim = await db.scalar(
            select(IntakeCommandClaim)
            .where(
                IntakeCommandClaim.session_id == session_id,
                IntakeCommandClaim.idempotency_key == command_id,
            )
            .with_for_update()
        )
        if claim is None:
            raise RepositoryError(RepositoryErrorCode.TRANSACTION_FAILED)
        if claim.status == "completed":
            if claim.response_payload != response:
                raise RepositoryError(RepositoryErrorCode.IDEMPOTENCY_KEY_REUSED)
            return
        claim.status = "completed"
        claim.output_state_version = state_version
        claim.response_payload = response
        claim.error_code = None
        claim.updated_at = func.now()


def _advance_response(
    *,
    session_id: uuid.UUID,
    state_version: int,
    current_stage: str,
    safety_record: ArtifactPayloadRecord,
    passed: bool,
    trace_id: str,
    reopened_for_safety: bool = False,
    safety_attempt: int = 1,
) -> dict[str, Any]:
    response: dict[str, Any] = {
        "session_id": str(session_id),
        "current_stage": current_stage,
        "from_stage": "safety",
        "state_version": state_version,
        "blocked_reason": None if passed else "safety_rule_blocked",
        "agent_name": "safety_review_subgraph",
        "trace_id": trace_id,
        "route": NODE_REVIEW_PLACEHOLDER,
        "artifact_refs": [
            {
                "kind": SAFETY_ARTIFACT_TYPE,
                "artifact_id": str(safety_record.artifact_id),
                "revision": safety_record.revision,
            }
        ],
        "gate_results": [
            {
                "gate_name": "safety_rule_engine",
                "decision": "passed" if passed else "failed",
                "policy_version": SAFETY_POLICY_VERSION,
            }
        ],
        "review_required": passed,
        "pending_review": passed,
        "safety_executed": True,
    }
    # 自动重开方标记：安全失败且尝试次数未耗尽 → 会话已重置回 syndrome，
    # advance 处理循环据此继续下一轮 reasoning 重开方，而非落 blocked。
    if reopened_for_safety:
        response["reopened_for_safety"] = True
        response["safety_attempt"] = safety_attempt
    return response


def _safety_projection_matches(
    projection: SafetyRuleRun | None,
    *,
    session_id: uuid.UUID,
    formula: FormulaAuthority,
    result: SafetyRuleResult,
    patient_snapshot: dict[str, Any],
    agent_run_id: uuid.UUID | None,
    trace_id: str | None,
) -> bool:
    """Bind every clinical SafetyRuleRun field to the artifact authority."""

    expected_formula_source = (
        "doctor_override"
        if formula.record.artifact_type == REVIEWED_FORMULA_ARTIFACT_TYPE
        else "agent_output"
    )
    return (
        projection is not None
        and projection.session_id == session_id
        and projection.passed == result.passed
        and projection.rule_version == result.rule_version
        and projection.formula_source == expected_formula_source
        and projection.agent_run_id == agent_run_id
        and projection.trace_id == trace_id
        and projection.formula_snapshot == formula.formula.model_dump(mode="json")
        and projection.normalized_formula
        == result.normalized_formula.model_dump(mode="json")
        and projection.issues
        == [item.model_dump(mode="json") for item in result.issues]
        and projection.patient_snapshot == patient_snapshot
    )


async def _load_safety_authority(
    repository: PostgresDomainRepository,
    session_id: uuid.UUID,
    formula: FormulaAuthority,
    safety_profile: SafetyProfileSchema | None,
    observations: tuple[Any, ...] = (),
) -> tuple[ArtifactPayloadRecord, SafetyRuleResult, uuid.UUID]:
    record = await repository.get_artifact_payload(
        session_id,
        artifact_type=SAFETY_ARTIFACT_TYPE,
        artifact_id=_stable_id(session_id, SAFETY_ARTIFACT_TYPE),
        status="current",
    )
    if record is None or record.payload_schema_version != SAFETY_PAYLOAD_SCHEMA_VERSION:
        raise RepositoryError(RepositoryErrorCode.ARTIFACT_PAYLOAD_INVALID)
    await _require_completed_producer(record)
    payload = record.payload
    if payload.get("kind") != SAFETY_ARTIFACT_TYPE or payload.get("formula_ref") != _formula_ref(formula):
        raise RepositoryError(RepositoryErrorCode.ARTIFACT_PAYLOAD_INVALID)
    try:
        result = SafetyRuleResult.model_validate(payload.get("result"))
        run_id = uuid.UUID(cast(str, payload.get("safety_rule_run_id")))
    except (ValidationError, TypeError, ValueError):
        raise RepositoryError(RepositoryErrorCode.ARTIFACT_PAYLOAD_INVALID) from None
    raw_agent_run_id = payload.get("agent_run_id")
    raw_trace_id = payload.get("trace_id")
    if (
        "agent_run_id" not in payload
        or not isinstance(raw_trace_id, str)
        or not 1 <= len(raw_trace_id) <= 64
    ):
        raise RepositoryError(RepositoryErrorCode.ARTIFACT_PAYLOAD_INVALID)
    try:
        agent_run_id = (
            None
            if raw_agent_run_id is None
            else uuid.UUID(cast(str, raw_agent_run_id))
        )
    except (TypeError, ValueError):
        raise RepositoryError(RepositoryErrorCode.ARTIFACT_PAYLOAD_INVALID) from None
    if safety_profile is None:
        raise RepositoryError(RepositoryErrorCode.ARTIFACT_PAYLOAD_INVALID)
    # 快照重算必须与写入侧同口径（含 domain observations 里的性别/年龄），
    # 否则 2026-08 的 PatientInfo 富集会让新旧快照不一致 → ARTIFACT_PAYLOAD_INVALID。
    patient_snapshot = _patient_info_from_domain(safety_profile, observations=observations).model_dump(
        mode="json",
        exclude={"name"},
    )
    factory = get_session_factory()
    async with factory() as db:
        projection = await db.get(SafetyRuleRun, run_id)
        if not _safety_projection_matches(
            projection,
            session_id=session_id,
            formula=formula,
            result=result,
            patient_snapshot=patient_snapshot,
            agent_run_id=agent_run_id,
            trace_id=raw_trace_id,
        ):
            raise RepositoryError(RepositoryErrorCode.ARTIFACT_PAYLOAD_INVALID)
    return record, result, run_id


def _pending_update(prepared: PreparedReview) -> dict[str, Any]:
    interrupt_id = f"{prepared.safety_record.artifact_id}:{prepared.safety_record.revision}"
    refs: list[ArtifactRef] = [
        {
            "kind": prepared.formula.record.artifact_type,
            "artifact_id": str(prepared.formula.record.artifact_id),
            "revision": prepared.formula.record.revision,
        },
        {
            "kind": SAFETY_ARTIFACT_TYPE,
            "artifact_id": str(prepared.safety_record.artifact_id),
            "revision": prepared.safety_record.revision,
        },
    ]
    return {
        "route": NODE_REVIEW_PLACEHOLDER,
        "domain_state_version": prepared.state_version,
        "artifact_refs": refs,
        "gate_results": [
            {
                "gate_name": "safety_rule_engine",
                "decision": "passed",
                "policy_version": SAFETY_POLICY_VERSION,
            }
        ],
        "pending_interrupt": {
            "kind": "doctor_review",
            "interrupt_id": interrupt_id,
            "resume_token_ref": "review_submission_ref",
        },
        "last_error": None,
    }


async def _prepared_from_current(session_id: uuid.UUID) -> PreparedReview:
    repository = PostgresDomainRepository(get_session_factory())
    state = await repository.get_state(session_id)
    meta = await _session_meta(session_id)
    # 安全引擎拦截（HIGH/BLOCKER）后会话停在 blocked；此时医生仍应能 review
    # 被拦方子（modify 调剂量 → 二次安全审核 → record），否则只能回滚重问。
    # 仅 safety_rule_blocked 放行；triage_hold / intake / reasoning 类拦截不可 review。
    blocked_safety = (
        meta.current_stage == "blocked"
        and meta.status == "blocked"
        and meta.blocked_reason == "safety_rule_blocked"
    )
    if (
        meta.agent_runtime != "langgraph"
        or meta.state_version != state.state_version
        or not (
            blocked_safety
            or (
                meta.current_stage == "review"
                and meta.status == "pending_review"
                and meta.pending_review
            )
        )
    ):
        raise InvalidStageTransitionError(
            message="当前会话没有待处理的 LangGraph Review interrupt",
            detail=f"session_id={session_id} current_stage={meta.current_stage} status={meta.status}",
            retryable=False,
        )
    formula = await _load_formula_authority(repository, session_id)
    safety_record, safety_result, safety_run_id = await _load_safety_authority(
        repository,
        session_id,
        formula,
        state.safety_profile,
        observations=state.observations,
    )
    # review 阶段但底层 safety 未通过（医生 modify 后被二次拦截的修正态）与
    # blocked 同权：仍允许继续 modify 修正，但禁止 confirm 绕过安全门。
    effective_blocked = blocked_safety or not safety_result.passed
    return PreparedReview(
        session_id=session_id,
        state_version=state.state_version,
        formula=formula,
        safety_record=safety_record,
        safety_result=safety_result,
        safety_rule_run_id=safety_run_id,
        interrupt_payload={
            "kind": "doctor_review",
            "request_artifact_id": str(safety_record.artifact_id),
            "request_revision": str(safety_record.revision),
            "request_digest": safety_record.content_digest,
            "state_version": str(state.state_version),
            "resume_token_ref": "review_submission_ref",
        },
        from_blocked_safety=effective_blocked,
    )


async def prepare_review_interrupt(state: XuanhuGraphState) -> dict[str, Any]:
    """Run Safety once and persist the review request before interrupting."""

    try:
        session_id = uuid.UUID(state.get("session_id", ""))
        run_id = uuid.UUID(state.get("run_id", ""))
    except (TypeError, ValueError):
        return _safe_error(state, "REVIEW_COMMAND_REF_INVALID", "review command refs are invalid")
    command_id = state.get("command_id", "")
    repository = PostgresDomainRepository(get_session_factory())
    domain_state = await repository.get_state(session_id)
    meta = await _session_meta(session_id)
    if meta.current_stage == "record":
        from app.services.langgraph_record import execute_record_command

        return await execute_record_command(state)
    if meta.current_stage == "review":
        try:
            return _pending_update(await _prepared_from_current(session_id))
        except (RepositoryError, InvalidStageTransitionError, SafetyReviewBlockedError) as exc:
            # A syntactically valid product command already owns a durable
            # IntakeCommandClaim.  Returning a graph-shaped error here would
            # leave that claim in ``running`` because no response is committed.
            # Raising delegates the terminal transition to the API's common
            # failure path, which marks both the claim and GraphRun failed.
            raise RepositoryError(RepositoryErrorCode.ARTIFACT_PAYLOAD_INVALID) from exc
    if (
        meta.agent_runtime != "langgraph"
        or meta.current_stage != "safety"
        or meta.status != "active"
        or meta.pending_review
        or meta.state_version != domain_state.state_version
    ):
        raise RepositoryError(RepositoryErrorCode.STATE_VERSION_CONFLICT)
    if not _safety_preconditions(domain_state.safety_profile):
        raise RepositoryError(RepositoryErrorCode.ARTIFACT_PAYLOAD_INVALID)

    formula = await _load_formula_authority(repository, session_id)
    assert domain_state.safety_profile is not None
    patient_info = _patient_info_from_domain(
        domain_state.safety_profile,
        observations=domain_state.observations,
    )
    factory = get_session_factory()
    async with factory() as safety_db:
        result = await SafetyRuleEngine(safety_db).evaluate(
            formula.formula, patient_info, observe_metric=True
        )

    safety_id = _stable_id(session_id, SAFETY_ARTIFACT_TYPE)
    latest = await repository.get_artifact_payload(
        session_id,
        artifact_type=SAFETY_ARTIFACT_TYPE,
        artifact_id=safety_id,
        status=None,
    )
    artifact = _artifact_revision(
        session_id=session_id,
        artifact_id=safety_id,
        artifact_type=SAFETY_ARTIFACT_TYPE,
        state_version=domain_state.state_version,
        run_id=run_id,
        latest=latest,
    )
    safety_run_id = uuid.uuid5(uuid.NAMESPACE_URL, f"xuanhu:safety-run:{run_id}")
    trace_id = _node_trace_id(state)
    payload: dict[str, object] = {
        "kind": SAFETY_ARTIFACT_TYPE,
        "formula_ref": _formula_ref(formula),
        "result": result.model_dump(mode="json"),
        "safety_rule_run_id": str(safety_run_id),
        "agent_run_id": None,
        "trace_id": trace_id,
    }
    payload_spec = _payload_spec(artifact, schema_version=SAFETY_PAYLOAD_SCHEMA_VERSION, payload=payload)
    # 自动重开方：安全失败且当前方子尝试次数未耗尽 → 重置回 syndrome 重开方（而非 blocked）。
    # 尝试次数按方子 revision 计数（_safety_attempt_count），新方子重置预算。
    safety_attempt = await _safety_attempt_count(
        repository,
        session_id,
        formula.record.revision,
    ) + 1
    # 3.1 自动重开方仅适用于可修正的 WARNING/HIGH（超剂量等，重开方让模型降剂量/换药）。
    # BLOCKER（过敏/禁忌/十八反十九畏等硬门禁）重开方无法消除不安全性——重开只会浪费
    # 模型调用并延迟 fail-closed 拦截，故含 BLOCKER 时立即落 blocked，不进入自动重开。
    has_blocker = any(item.severity == Severity.BLOCKER for item in result.issues)
    reopened_for_safety = (
        (not result.passed)
        and not has_blocker
        and safety_attempt < MAX_SAFETY_REOPEN_ATTEMPTS
    )
    delta = DomainDelta(
        delta_id=uuid.uuid5(uuid.NAMESPACE_URL, f"xuanhu:delta:safety:{run_id}"),
        run_id=run_id,
        session_id=session_id,
        expected_state_version=domain_state.state_version,
        artifact_revisions=(artifact,),
        invalidate_artifact_ids=(
            (formula.record.artifact_id,) if reopened_for_safety else ()
        ),
    )
    if reopened_for_safety:
        # 拦截原因注入重新开方：模型看到 issues 自动降剂量/换药。失效方子保留辨证。
        target_stage = "syndrome"
        target_status = "active"
        feedback = _safety_feedback_text(result)
    else:
        target_stage = "review" if result.passed else "blocked"
        target_status = "pending_review" if result.passed else "blocked"
        feedback = None
    await repository.commit(
        delta,
        _verification_context(
            delta,
            domain_state,
            stage="safety",
            idempotency_key=f"{command_id}:safety",
            trace_id=trace_id,
        ),
        graph_version=DEFAULT_GRAPH_VERSION,
        gate_results=(
            _gate(
                "safety_rule_engine",
                domain_state.state_version,
                GateDecision.PASSED if result.passed else GateDecision.FAILED,
                {
                    "artifact_digest": payload_spec.content_digest,
                    "formula_artifact_id": str(formula.record.artifact_id),
                    "formula_revision": formula.record.revision,
                    "issue_count": len(result.issues),
                },
            ),
        ),
        graph_steps=(
            GraphStepSpec(step_name="safety_rule_engine", status="completed", metadata={}),
            GraphStepSpec(
                step_name=(
                    "safety_reopen"
                    if reopened_for_safety
                    else "doctor_review_interrupt"
                    if result.passed
                    else "safety_blocked"
                ),
                status="completed",
                metadata={},
            ),
        ),
        artifact_payloads=(payload_spec,),
        safety_rule_runs=(
            SafetyRuleRunSpec(
                safety_rule_run_id=safety_run_id,
                session_id=session_id,
                formula_source="agent_output",
                passed=result.passed,
                issues=[cast(dict[str, object], item.model_dump(mode="json")) for item in result.issues],
                formula_snapshot=cast(dict[str, object], formula.formula.model_dump(mode="json")),
                normalized_formula=cast(dict[str, object], result.normalized_formula.model_dump(mode="json")),
                patient_snapshot=cast(
                    dict[str, object],
                    patient_info.model_dump(mode="json", exclude={"name"}),
                ),
                rule_version=result.rule_version,
                trace_id=trace_id,
            ),
        ),
        audit_events=(
            AuditEventSpec(
                event_id=uuid.uuid5(
                    uuid.NAMESPACE_URL,
                    f"xuanhu:audit:langgraph-safety-prepared:{run_id}",
                ),
                session_id=session_id,
                event_type="langgraph.safety_prepared",
                actor_type="system",
                actor_id=None,
                payload={
                    "safety_artifact_id": str(artifact.artifact_id),
                    "safety_revision": artifact.revision,
                    "safety_rule_run_id": str(safety_run_id),
                    "passed": result.passed,
                    "reopened_for_safety": reopened_for_safety,
                    "safety_attempt": safety_attempt,
                },
                trace_id=trace_id,
            ),
        ),
        session_updates=_session_updates(
            current_stage=target_stage,
            status=target_status,
            pending_review=result.passed and not reopened_for_safety,
            state_version=domain_state.state_version + 1,
            route=(
                "safety_reopen"
                if reopened_for_safety
                else "review_required"
                if result.passed
                else "safety_blocked"
            ),
            blocked_reason=None if result.passed or reopened_for_safety else "safety_rule_blocked",
            preserve_advance=_preserved_advance(meta),
            review_feedback=feedback,
        ),
        outbox_event_type=(
            "safety.reopened.v1"
            if reopened_for_safety
            else "review.required.v1"
            if result.passed
            else "safety.blocked.v1"
        ),
        outbox_payload={
            "session_id": str(session_id),
            "safety_artifact_id": str(artifact.artifact_id),
            "safety_revision": artifact.revision,
            "passed": result.passed,
            "reopened_for_safety": reopened_for_safety,
            "issue_count": len(result.issues),
            "input_state_version": domain_state.state_version,
            "output_state_version": domain_state.state_version + 1,
        },
    )
    safety_record = await repository.get_artifact_payload(
        session_id,
        artifact_type=SAFETY_ARTIFACT_TYPE,
        artifact_id=safety_id,
        revision=artifact.revision,
        status="current",
    )
    if safety_record is None:
        raise RepositoryError(RepositoryErrorCode.TRANSACTION_FAILED)
    response = _advance_response(
        session_id=session_id,
        state_version=domain_state.state_version + 1,
        current_stage=target_stage,
        safety_record=safety_record,
        passed=result.passed and not reopened_for_safety,
        trace_id=trace_id,
        reopened_for_safety=reopened_for_safety,
        safety_attempt=safety_attempt,
    )
    await _complete_advance_claim(
        session_id=session_id,
        command_id=command_id,
        response=response,
        state_version=domain_state.state_version + 1,
    )
    if not result.passed and not reopened_for_safety:
        # safety 硬门禁不通过（且未自动重开）：路由到 blocked 终态（与 session
        # stage=blocked 一致），否则 checkpoint 记成 review_placeholder →
        # recover 时状态错乱 → 死循环。
        return {
            "route": NODE_BLOCKED_TERMINAL,
            "domain_state_version": domain_state.state_version + 1,
            "artifact_refs": response["artifact_refs"],
            "gate_results": response["gate_results"],
            "pending_interrupt": None,
            "last_error": {
                "code": "SAFETY_GATE_BLOCKED",
                "trace_id": trace_id,
                "detail": "deterministic safety gate blocked the formula",
            },
        }
    if reopened_for_safety:
        # 自动重开方：会话已重置回 syndrome/active，REVIEW 图命令在此干净结束，
        # advance 处理循环据此发起下一轮 reasoning 重开方。
        return {
            "route": NODE_REVIEW_PLACEHOLDER,
            "domain_state_version": domain_state.state_version + 1,
            "artifact_refs": response["artifact_refs"],
            "gate_results": response["gate_results"],
            "pending_interrupt": None,
            "last_error": None,
            "reopened_for_safety": True,
            "safety_attempt": safety_attempt,
        }
    return _pending_update(await _prepared_from_current(session_id))


async def load_prepared_review(state: XuanhuGraphState) -> PreparedReview:
    """Reload authoritative review references without completing the node.

    Authority or infrastructure failures intentionally propagate.  Turning
    them into a normal graph update would consume the pending interrupt and
    leave PostgreSQL claiming ``pending_review`` with no resumable checkpoint.
    """

    try:
        session_id = uuid.UUID(state.get("session_id", ""))
    except (TypeError, ValueError):
        raise RepositoryError(RepositoryErrorCode.ARTIFACT_PAYLOAD_INVALID) from None
    return await _prepared_from_current(session_id)


def _submission_payload(record: ArtifactPayloadRecord) -> tuple[str, uuid.UUID, dict[str, Any]]:
    if record.payload_schema_version != REVIEW_SUBMISSION_SCHEMA_VERSION:
        raise RepositoryError(RepositoryErrorCode.ARTIFACT_PAYLOAD_INVALID)
    payload = record.payload
    action = payload.get("action")
    review_id = payload.get("review_id")
    if (
        payload.get("kind") != REVIEW_SUBMISSION_ARTIFACT_TYPE
        or action not in {"confirm", "modify", "reject", "request_more_info"}
        or not isinstance(review_id, str)
    ):
        raise RepositoryError(RepositoryErrorCode.ARTIFACT_PAYLOAD_INVALID)
    try:
        parsed_id = uuid.UUID(review_id)
    except ValueError:
        raise RepositoryError(RepositoryErrorCode.ARTIFACT_PAYLOAD_INVALID) from None
    if parsed_id != record.artifact_id:
        raise RepositoryError(RepositoryErrorCode.ARTIFACT_PAYLOAD_INVALID)
    # 上方守卫已确保 action 为四个已知字符串之一。
    return action, parsed_id, cast(dict[str, Any], payload)


def _is_digest(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _valid_submission_refs(payload: dict[str, Any]) -> bool:
    safety_ref = payload.get("safety_ref")
    formula_ref = payload.get("formula_ref")
    if (
        not isinstance(safety_ref, dict)
        or set(safety_ref)
        != {
            "artifact_id",
            "revision",
            "content_digest",
            "safety_rule_run_id",
        }
        or not isinstance(formula_ref, dict)
        or set(formula_ref)
        != {"artifact_type", "artifact_id", "revision", "content_digest"}
        or formula_ref.get("artifact_type")
        not in {FORMULA_ARTIFACT_TYPE, REVIEWED_FORMULA_ARTIFACT_TYPE}
    ):
        return False
    try:
        uuid.UUID(cast(str, safety_ref.get("artifact_id")))
        uuid.UUID(cast(str, safety_ref.get("safety_rule_run_id")))
        uuid.UUID(cast(str, formula_ref.get("artifact_id")))
    except (TypeError, ValueError):
        return False
    return (
        isinstance(safety_ref.get("revision"), int)
        and safety_ref["revision"] >= 1
        and _is_digest(safety_ref.get("content_digest"))
        and isinstance(formula_ref.get("revision"), int)
        and formula_ref["revision"] >= 1
        and _is_digest(formula_ref.get("content_digest"))
    )


async def apply_review_resume(
    state: XuanhuGraphState,
    *,
    prepared: PreparedReview,
    resume_value: object,
) -> dict[str, Any]:
    """Validate the persisted submission reference and apply its decision."""

    if not isinstance(resume_value, dict) or set(resume_value) != {"review_submission_ref"}:
        _reject_resume("REVIEW_RESUME_REF_INVALID", "review resume ref is invalid")
    raw_ref = resume_value.get("review_submission_ref")
    if not isinstance(raw_ref, str):
        _reject_resume("REVIEW_RESUME_REF_INVALID", "review resume ref is invalid")
    try:
        submission_id = uuid.UUID(raw_ref)
    except ValueError:
        _reject_resume("REVIEW_RESUME_REF_INVALID", "review resume ref is invalid")

    repository = PostgresDomainRepository(get_session_factory())
    submission = await repository.get_artifact_payload(
        prepared.session_id,
        artifact_type=REVIEW_SUBMISSION_ARTIFACT_TYPE,
        artifact_id=submission_id,
        revision=1,
        status=None,
    )
    if submission is None:
        _reject_resume("REVIEW_SUBMISSION_NOT_FOUND", "review submission is unavailable")
    try:
        action, review_id, payload = _submission_payload(submission)
    except RepositoryError:
        _reject_resume("REVIEW_SUBMISSION_INVALID", "review submission is invalid")
    expected_safety_ref = {
        "artifact_id": str(prepared.safety_record.artifact_id),
        "revision": prepared.safety_record.revision,
        "content_digest": prepared.safety_record.content_digest,
        "safety_rule_run_id": str(prepared.safety_rule_run_id),
    }
    if payload.get("safety_ref") != expected_safety_ref:
        _reject_resume("REVIEW_SUBMISSION_STALE", "review submission is stale")
    expected_formula_ref = _formula_ref(prepared.formula)
    if payload.get("formula_ref") != expected_formula_ref:
        _reject_resume("REVIEW_SUBMISSION_STALE", "review submission formula is stale")

    factory = get_session_factory()
    async with factory() as db:
        projection = await db.get(DoctorReview, review_id)
        if (
            projection is None
            or projection.session_id != prepared.session_id
            or projection.action != action
            or projection.safety_rule_run_id != prepared.safety_rule_run_id
            or projection.feedback != payload.get("feedback")
            or projection.original_formula != payload.get("original_formula")
            or projection.formula_override != payload.get("formula_override")
            or projection.reviewed_by != payload.get("reviewed_by")
        ):
            _reject_resume("REVIEW_PROJECTION_INVALID", "review projection is invalid")

    domain_state = await repository.get_state(prepared.session_id)
    meta = await _session_meta(prepared.session_id)
    if (
        submission.status != "current"
        or meta.current_stage != "review"
        or not meta.pending_review
        or meta.state_version != domain_state.state_version
    ):
        _reject_resume("REVIEW_STATE_CONFLICT", "review state changed before resume")
    run_id = uuid.uuid5(uuid.NAMESPACE_URL, f"xuanhu:review-apply:{submission_id}")
    artifact_id = uuid.uuid5(uuid.NAMESPACE_URL, f"xuanhu:doctor-review:{submission_id}")
    artifact = _artifact_revision(
        session_id=prepared.session_id,
        artifact_id=artifact_id,
        artifact_type=DOCTOR_REVIEW_ARTIFACT_TYPE,
        state_version=domain_state.state_version,
        run_id=run_id,
        latest=None,
    )
    doctor_payload: dict[str, object] = {
        "kind": DOCTOR_REVIEW_ARTIFACT_TYPE,
        "review_id": str(review_id),
        "action": action,
        "submission_ref": {
            "artifact_id": str(submission.artifact_id),
            "revision": submission.revision,
            "content_digest": submission.content_digest,
        },
        "safety_ref": expected_safety_ref,
        "formula_ref": expected_formula_ref,
        "reviewed_by": projection.reviewed_by,
        "reviewed_at": projection.created_at.isoformat(),
        "feedback": projection.feedback,
        "original_formula": projection.original_formula,
        "formula_override": projection.formula_override,
    }
    payload_spec = _payload_spec(artifact, schema_version=DOCTOR_REVIEW_SCHEMA_VERSION, payload=doctor_payload)
    invalidations: list[uuid.UUID] = [
        submission.artifact_id,
        *(
            item.artifact_id
            for item in domain_state.artifacts
            if item.status is ArtifactStatus.CURRENT
            and item.artifact_type == DOCTOR_REVIEW_ARTIFACT_TYPE
        ),
    ]
    if action in {"reject", "request_more_info"}:
        invalidations.extend(
            item.artifact_id
            for item in domain_state.artifacts
            if item.status is ArtifactStatus.CURRENT
            and item.artifact_type
            in {
                FORMULA_ARTIFACT_TYPE,
                REVIEWED_FORMULA_ARTIFACT_TYPE,
                SAFETY_ARTIFACT_TYPE,
                "syndrome_draft" if action in {"request_more_info", "reject"} else "__none__",
            }
        )
    invalidation_ids = tuple(dict.fromkeys(invalidations))
    delta = DomainDelta(
        delta_id=uuid.uuid5(uuid.NAMESPACE_URL, f"xuanhu:delta:review-apply:{submission_id}"),
        run_id=run_id,
        session_id=prepared.session_id,
        expected_state_version=domain_state.state_version,
        artifact_revisions=(artifact,),
        invalidate_artifact_ids=invalidation_ids,
    )
    target_stage = "record" if action in {"confirm", "modify"} else "syndrome"
    # 保留 intake→syndrome 的 advance 出处：回到 syndrome 后重新 advance
    # 时 reasoning 预检复用 completeness gate，否则 REASONING_PRECHECK_FAILED。
    preserve_advance = _preserved_advance(meta)
    trace_id = _node_trace_id(state)
    commit = await repository.commit(
        delta,
        _verification_context(
            delta,
            domain_state,
            stage="review",
            idempotency_key=f"review-apply:{submission_id}",
            trace_id=trace_id,
        ),
        graph_version=DEFAULT_GRAPH_VERSION,
        gate_results=(
            _gate(
                "doctor_review",
                domain_state.state_version,
                GateDecision.PASSED if action in {"confirm", "modify"} else GateDecision.BLOCKED,
                {
                    "review_id": str(review_id),
                    "action": action,
                    "submission_digest": submission.content_digest,
                },
            ),
        ),
        graph_steps=(GraphStepSpec(step_name="apply_doctor_review", status="completed", metadata={}),),
        artifact_payloads=(payload_spec,),
        audit_events=(
            AuditEventSpec(
                event_id=uuid.uuid5(
                    uuid.NAMESPACE_URL,
                    f"xuanhu:audit:langgraph-review-applied:{submission_id}",
                ),
                session_id=prepared.session_id,
                event_type="langgraph.review_applied",
                actor_type="doctor",
                actor_id=projection.reviewed_by,
                payload={
                    "review_id": str(review_id),
                    "action": action,
                    "submission_digest": submission.content_digest,
                    "doctor_review_artifact_id": str(artifact.artifact_id),
                },
                trace_id=trace_id,
            ),
        ),
        session_updates=_session_updates(
            current_stage=target_stage,
            status="active",
            pending_review=False,
            state_version=domain_state.state_version + 1,
            route=f"review_{action}",
            preserve_advance=preserve_advance,
            # 回退反馈持久化：重新辨证/开方时注入模型输入。
            review_feedback=payload.get("feedback") if action in {"reject", "request_more_info"} else None,
        ),
        outbox_event_type="doctor.review_applied.v1",
        outbox_payload={
            "session_id": str(prepared.session_id),
            "review_id": str(review_id),
            "action": action,
            "target_stage": target_stage,
            "input_state_version": domain_state.state_version,
            "output_state_version": domain_state.state_version + 1,
        },
    )
    refs: list[ArtifactRef] = [
        {
            "kind": DOCTOR_REVIEW_ARTIFACT_TYPE,
            "artifact_id": str(artifact.artifact_id),
            "revision": artifact.revision,
        }
    ]
    return {
        "route": NODE_REVIEW_PLACEHOLDER,
        "domain_state_version": commit.output_state_version,
        "artifact_refs": refs,
        "gate_results": [
            {
                "gate_name": "doctor_review",
                "decision": "passed" if action in {"confirm", "modify"} else "blocked",
                "policy_version": REVIEW_POLICY_VERSION,
            }
        ],
        "pending_interrupt": None,
        "last_error": None,
    }


def _formula_from_override(override: FormulaOverride) -> FormulaResult:
    return FormulaResult(
        name=override.name or "reviewer_override",
        composition=[
            HerbDose(herb=item.herb, dose=item.dose, unit=item.unit, note=item.note)
            for item in override.composition
        ],
        source=override.source,
        rationale=override.rationale or "reviewer supplied full-formula override",
        citations=[],
    )


class LangGraphReviewService:
    """Stage once, then safely resume or repair the session's Review checkpoint."""

    def __init__(self, db: AsyncSession) -> None:
        self._db = db

    @staticmethod
    def _expected_override(request: ReviewRequest) -> dict[str, Any] | None:
        if request.action != "modify":
            return None
        if request.formula_override is None:
            raise FormulaOverrideRequiredError()
        return _formula_from_override(request.formula_override).model_dump(mode="json")

    async def _load_validated_submission(
        self,
        *,
        session_id: uuid.UUID,
        submission_id: uuid.UUID,
        request: ReviewRequest,
        doctor_id: str | None,
    ) -> _ValidatedSubmission | None:
        repository = PostgresDomainRepository(get_session_factory())
        existing = await repository.get_artifact_payload(
            session_id,
            artifact_type=REVIEW_SUBMISSION_ARTIFACT_TYPE,
            artifact_id=submission_id,
            revision=1,
            status=None,
        )
        if existing is None:
            return None
        action, review_id, payload = _submission_payload(existing)
        expected_override = self._expected_override(request)
        factory = get_session_factory()
        async with factory() as db:
            projection = await db.get(DoctorReview, review_id)
        safety_ref = payload.get("safety_ref")
        if (
            action != request.action
            or not _valid_submission_refs(payload)
            or not isinstance(payload.get("original_formula"), dict)
            or payload.get("feedback") != request.feedback
            or payload.get("formula_override") != expected_override
            or payload.get("reviewed_by") != doctor_id
            or projection is None
            or projection.session_id != session_id
            or projection.action != action
            or projection.original_formula != payload.get("original_formula")
            or projection.feedback != request.feedback
            or projection.formula_override != expected_override
            or projection.reviewed_by != doctor_id
            or projection.safety_rule_run_id is None
            or not isinstance(safety_ref, dict)
            or str(projection.safety_rule_run_id)
            != safety_ref.get("safety_rule_run_id")
        ):
            raise RepositoryError(RepositoryErrorCode.IDEMPOTENCY_KEY_REUSED)
        return _ValidatedSubmission(
            record=existing,
            projection=projection,
            payload=payload,
            action=action,
        )

    async def _require_applied_review_artifact(
        self,
        *,
        session_id: uuid.UUID,
        submission: _ValidatedSubmission,
    ) -> None:
        artifact_id = uuid.uuid5(
            uuid.NAMESPACE_URL,
            f"xuanhu:doctor-review:{submission.record.artifact_id}",
        )
        repository = PostgresDomainRepository(get_session_factory())
        applied = await repository.get_artifact_payload(
            session_id,
            artifact_type=DOCTOR_REVIEW_ARTIFACT_TYPE,
            artifact_id=artifact_id,
            revision=1,
            status="current",
        )
        projection = submission.projection
        expected_payload: dict[str, object] = {
            "kind": DOCTOR_REVIEW_ARTIFACT_TYPE,
            "review_id": str(projection.id),
            "action": submission.action,
            "submission_ref": {
                "artifact_id": str(submission.record.artifact_id),
                "revision": submission.record.revision,
                "content_digest": submission.record.content_digest,
            },
            "safety_ref": submission.payload["safety_ref"],
            "formula_ref": submission.payload["formula_ref"],
            "reviewed_by": projection.reviewed_by,
            "reviewed_at": projection.created_at.isoformat(),
            "feedback": projection.feedback,
            "original_formula": projection.original_formula,
            "formula_override": projection.formula_override,
        }
        if (
            applied is None
            or applied.payload_schema_version != DOCTOR_REVIEW_SCHEMA_VERSION
            or applied.payload != expected_payload
        ):
            raise RepositoryError(RepositoryErrorCode.ARTIFACT_PAYLOAD_INVALID)

    async def _continue_existing_submission(
        self,
        *,
        session_id: uuid.UUID,
        submission_id: uuid.UUID,
        request: ReviewRequest,
        doctor_id: str | None,
        shared_runtime: Any | None,
        allow_request_local_runtime: bool,
    ) -> ReviewResponse | None:
        """Continue or verify one deterministic durable review submission."""

        existing = await self._load_validated_submission(
            session_id=session_id,
            submission_id=submission_id,
            request=request,
            doctor_id=doctor_id,
        )
        if existing is None:
            return None
        if existing.record.status == "current":
            await self._resume(
                session_id=str(session_id),
                submission_id=submission_id,
                shared_runtime=shared_runtime,
                allow_request_local_runtime=allow_request_local_runtime,
            )
            refreshed = await self._load_validated_submission(
                session_id=session_id,
                submission_id=submission_id,
                request=request,
                doctor_id=doctor_id,
            )
            if refreshed is None or refreshed.record.status == "current":
                raise RepositoryError(RepositoryErrorCode.TRANSACTION_FAILED)
            existing = refreshed
        await self._require_applied_review_artifact(
            session_id=session_id,
            submission=existing,
        )
        return await self._response(session_id, submission_id, request)

    async def resolve_durable_outcome(
        self,
        session_id: str,
        request: ReviewRequest,
        *,
        doctor_id: str | None,
        idempotency_key: str,
        shared_runtime: Any | None,
        allow_request_local_runtime: bool,
    ) -> dict[str, Any] | None:
        """Return only a fully verified durable result for this exact request."""

        sid = uuid.UUID(session_id)
        submission_id = uuid.uuid5(
            uuid.NAMESPACE_URL,
            f"xuanhu:review-submission:{sid}:{idempotency_key}",
        )
        response = await self._continue_existing_submission(
            session_id=sid,
            submission_id=submission_id,
            request=request,
            doctor_id=doctor_id,
            shared_runtime=shared_runtime,
            allow_request_local_runtime=allow_request_local_runtime,
        )
        return None if response is None else response.model_dump(mode="json")

    async def review(
        self,
        session_id: str,
        request: ReviewRequest,
        *,
        doctor_id: str | None,
        trace_id: str,
        x_state_version: int | None,
        idempotency_key: str,
        shared_runtime: Any | None,
        allow_request_local_runtime: bool,
    ) -> ReviewResponse:
        sid = uuid.UUID(session_id)
        if request.action == "modify" and request.formula_override is None:
            raise FormulaOverrideRequiredError()
        submission_id = uuid.uuid5(
            uuid.NAMESPACE_URL,
            f"xuanhu:review-submission:{sid}:{idempotency_key}",
        )
        replay = await self._continue_existing_submission(
            session_id=sid,
            submission_id=submission_id,
            request=request,
            doctor_id=doctor_id,
            shared_runtime=shared_runtime,
            allow_request_local_runtime=allow_request_local_runtime,
        )
        if replay is not None:
            return replay
        prepared = await _prepared_from_current(sid)
        meta = await _session_meta(sid)
        if meta.status == "terminated":
            raise SessionTerminatedError(detail=f"session_id={session_id} terminated")
        # 安全拦截（blocked）时 confirm 会绕过确定性安全门；医生必须 modify
        # 调整剂量触发二次审核，或用 reject / request_more_info 回退重问。
        if prepared.from_blocked_safety and request.action == "confirm":
            raise SafetyReviewBlockedError(
                issues=[item.model_dump(mode="json") for item in prepared.safety_result.issues],
                detail=f"session_id={session_id} safety-blocked formula cannot be confirmed; use modify",
            )
        if x_state_version is not None and x_state_version != meta.state_version:
            raise InvalidStateVersionError(
                detail=f"session_id={session_id} client version {x_state_version} != server version {meta.state_version}",
                retryable=True,
            )
        repository = PostgresDomainRepository(get_session_factory())
        domain_state = await repository.get_state(sid)
        run_id = uuid.uuid5(uuid.NAMESPACE_URL, f"xuanhu:review-submit:{sid}:{idempotency_key}")
        artifacts: list[ArtifactRevisionSchema] = []
        payloads: list[ArtifactPayloadSpec] = []
        safety_specs: list[SafetyRuleRunSpec] = []
        gates: list[GateResultSchema] = []
        formula = prepared.formula
        safety_record = prepared.safety_record
        safety_result = prepared.safety_result
        safety_run_id = prepared.safety_rule_run_id

        if request.action == "modify":
            assert request.formula_override is not None
            override_formula = _formula_from_override(request.formula_override)
            result = await SafetyRuleEngine(self._db).evaluate(
                override_formula,
                _patient_info_from_domain(
                    cast(SafetyProfileSchema, domain_state.safety_profile),
                    observations=domain_state.observations,
                ),
                observe_metric=True,
            )
            # A blocked override must remain auditable without replacing the
            # last passed Formula/Safety authority.  Otherwise the next
            # review command could no longer load its pending interrupt and a
            # clinician would be unable to correct the override.  Passed
            # overrides use the stable authoritative line; failed overrides
            # use a separate attempt-only line that no authority loader reads.
            reviewed_type, safety_type = _review_artifact_types(safety_passed=result.passed)
            reviewed_id = _stable_id(sid, reviewed_type)
            latest_reviewed = await repository.get_artifact_payload(
                sid,
                artifact_type=reviewed_type,
                artifact_id=reviewed_id,
                status=None,
            )
            reviewed_artifact = _artifact_revision(
                session_id=sid,
                artifact_id=reviewed_id,
                artifact_type=reviewed_type,
                state_version=domain_state.state_version,
                run_id=run_id,
                latest=latest_reviewed,
            )
            reviewed_payload: dict[str, object] = {
                "kind": reviewed_type,
                "formula": override_formula.model_dump(mode="json"),
                "source_formula_ref": _formula_ref(formula),
                "review_command_ref": str(submission_id),
            }
            reviewed_spec = _payload_spec(
                reviewed_artifact,
                schema_version=REVIEWED_FORMULA_SCHEMA_VERSION,
                payload=reviewed_payload,
            )
            reviewed_authority = FormulaAuthority(
                ArtifactPayloadRecord(
                    row_id=uuid.UUID(int=0),
                    artifact_revision_row_id=uuid.UUID(int=0),
                    session_id=sid,
                    artifact_id=reviewed_artifact.artifact_id,
                    artifact_type=reviewed_artifact.artifact_type,
                    revision=reviewed_artifact.revision,
                    input_state_version=reviewed_artifact.input_state_version,
                    status="current",
                    produced_by_run_id=run_id,
                    parent_revision_id=reviewed_artifact.parent_revision_id,
                    parent_revision=reviewed_artifact.parent_revision,
                    payload_schema_version=reviewed_spec.payload_schema_version,
                    payload=reviewed_spec.payload,
                    content_digest=reviewed_spec.content_digest,
                ),
                override_formula,
            )
            latest_safety = await repository.get_artifact_payload(
                sid,
                artifact_type=safety_type,
                artifact_id=_stable_id(sid, safety_type),
                status=None,
            )
            safety_artifact = _artifact_revision(
                session_id=sid,
                artifact_id=_stable_id(sid, safety_type),
                artifact_type=safety_type,
                state_version=domain_state.state_version,
                run_id=run_id,
                latest=latest_safety,
            )
            safety_run_id = uuid.uuid5(uuid.NAMESPACE_URL, f"xuanhu:safety-run:{run_id}")
            safety_trace_id = _bounded_trace(trace_id)
            safety_payload: dict[str, object] = {
                "kind": safety_type,
                "formula_ref": _formula_ref(reviewed_authority),
                "result": result.model_dump(mode="json"),
                "safety_rule_run_id": str(safety_run_id),
                "agent_run_id": None,
                "trace_id": safety_trace_id,
            }
            safety_spec = _payload_spec(
                safety_artifact,
                schema_version=SAFETY_PAYLOAD_SCHEMA_VERSION,
                payload=safety_payload,
            )
            artifacts.extend((reviewed_artifact, safety_artifact))
            payloads.extend((reviewed_spec, safety_spec))
            formula = reviewed_authority
            safety_result = result
            safety_record = reviewed_authority.record.model_copy(
                update={
                    "artifact_id": safety_artifact.artifact_id,
                    "artifact_type": safety_artifact.artifact_type,
                    "revision": safety_artifact.revision,
                    "input_state_version": safety_artifact.input_state_version,
                    "payload_schema_version": safety_spec.payload_schema_version,
                    "payload": safety_spec.payload,
                    "content_digest": safety_spec.content_digest,
                }
            )
            safety_specs.append(
                SafetyRuleRunSpec(
                    safety_rule_run_id=safety_run_id,
                    session_id=sid,
                    formula_source="doctor_override",
                    passed=result.passed,
                    issues=[cast(dict[str, object], item.model_dump(mode="json")) for item in result.issues],
                    formula_snapshot=cast(dict[str, object], override_formula.model_dump(mode="json")),
                    normalized_formula=cast(dict[str, object], result.normalized_formula.model_dump(mode="json")),
                    patient_snapshot=cast(
                        dict[str, object],
                        _patient_info_from_domain(
                            cast(SafetyProfileSchema, domain_state.safety_profile),
                            observations=domain_state.observations,
                        ).model_dump(mode="json", exclude={"name"}),
                    ),
                    rule_version=result.rule_version,
                    trace_id=safety_trace_id,
                )
            )
            gates.append(
                _gate(
                    "safety_rule_engine",
                    domain_state.state_version,
                    GateDecision.PASSED if result.passed else GateDecision.FAILED,
                    {
                        "artifact_digest": safety_spec.content_digest,
                        "formula_artifact_id": str(reviewed_artifact.artifact_id),
                        "formula_revision": reviewed_artifact.revision,
                        "issue_count": len(result.issues),
                    },
                )
            )

        # 仅 modify 的二次审核结果可触发该分支；blocked 会话的原始 safety 为
        # failed，reject / request_more_info 若误入此处会提交空 delta。
        if request.action == "modify" and not safety_result.passed:
            delta = DomainDelta(
                delta_id=uuid.uuid5(uuid.NAMESPACE_URL, f"xuanhu:delta:review-recheck:{run_id}"),
                run_id=run_id,
                session_id=sid,
                expected_state_version=domain_state.state_version,
                artifact_revisions=tuple(artifacts),
            )
            await repository.commit(
                delta,
                _verification_context(
                    delta,
                    domain_state,
                    stage="review",
                    idempotency_key=f"review-recheck:{idempotency_key}",
                    trace_id=_bounded_trace(trace_id),
                ),
                graph_version=DEFAULT_GRAPH_VERSION,
                gate_results=tuple(gates),
                graph_steps=(GraphStepSpec(step_name="review_safety_recheck", status="completed", metadata={}),),
                artifact_payloads=tuple(payloads),
                safety_rule_runs=tuple(safety_specs),
                session_updates=_session_updates(
                    current_stage="review",
                    status="pending_review",
                    pending_review=True,
                    state_version=domain_state.state_version + 1,
                    route="modify_safety_blocked",
                    preserve_advance=_preserved_advance(meta),
                ),
                outbox_event_type="review.safety_recheck_blocked.v1",
                outbox_payload={
                    "session_id": session_id,
                    "issue_count": len(safety_result.issues),
                    "input_state_version": domain_state.state_version,
                    "output_state_version": domain_state.state_version + 1,
                },
            )
            raise SafetyReviewBlockedError(
                detail=f"session_id={session_id} modified formula failed deterministic safety recheck",
                issues=[item.model_dump(mode="json") for item in safety_result.issues],
            )

        submission_artifact = _artifact_revision(
            session_id=sid,
            artifact_id=submission_id,
            artifact_type=REVIEW_SUBMISSION_ARTIFACT_TYPE,
            state_version=domain_state.state_version,
            run_id=run_id,
            latest=None,
        )
        safety_ref: dict[str, object] = {
            "artifact_id": str(safety_record.artifact_id),
            "revision": safety_record.revision,
            "content_digest": safety_record.content_digest,
            "safety_rule_run_id": str(safety_run_id),
        }
        original_formula = cast(
            dict[str, object],
            prepared.formula.formula.model_dump(mode="json"),
        )
        override_json = (
            cast(dict[str, object], formula.formula.model_dump(mode="json"))
            if request.action == "modify"
            else None
        )
        submission_payload: dict[str, object] = {
            "kind": REVIEW_SUBMISSION_ARTIFACT_TYPE,
            "review_id": str(submission_id),
            "action": request.action,
            "safety_ref": safety_ref,
            "formula_ref": _formula_ref(formula),
            "feedback": request.feedback,
            "original_formula": original_formula,
            "formula_override": override_json,
            "reviewed_by": doctor_id,
        }
        submission_spec = _payload_spec(
            submission_artifact,
            schema_version=REVIEW_SUBMISSION_SCHEMA_VERSION,
            payload=submission_payload,
        )
        artifacts.append(submission_artifact)
        payloads.append(submission_spec)
        gates.append(
            _gate(
                "review_submission",
                domain_state.state_version,
                GateDecision.PASSED,
                {
                    "submission_digest": submission_spec.content_digest,
                    "action": request.action,
                },
            )
        )
        delta = DomainDelta(
            delta_id=uuid.uuid5(uuid.NAMESPACE_URL, f"xuanhu:delta:review-submit:{run_id}"),
            run_id=run_id,
            session_id=sid,
            expected_state_version=domain_state.state_version,
            artifact_revisions=tuple(artifacts),
        )
        await repository.commit(
            delta,
            _verification_context(
                delta,
                domain_state,
                stage="review",
                idempotency_key=f"review-submit:{idempotency_key}",
                trace_id=_bounded_trace(trace_id),
            ),
            graph_version=DEFAULT_GRAPH_VERSION,
            gate_results=tuple(gates),
            graph_steps=(GraphStepSpec(step_name="persist_review_submission", status="completed", metadata={}),),
            artifact_payloads=tuple(payloads),
            safety_rule_runs=tuple(safety_specs),
            doctor_reviews=(
                DoctorReviewSpec(
                    review_id=submission_id,
                    session_id=sid,
                    safety_rule_run_id=safety_run_id,
                    action=request.action,
                    original_formula=original_formula,
                    formula_override=override_json,
                    feedback=request.feedback,
                    reviewed_by=doctor_id,
                ),
            ),
            audit_events=(
                AuditEventSpec(
                    event_id=uuid.uuid5(uuid.NAMESPACE_URL, f"xuanhu:audit:doctor-reviewed:{submission_id}"),
                    session_id=sid,
                    event_type="doctor.reviewed",
                    actor_type="doctor",
                    actor_id=doctor_id,
                    payload={
                        "review_id": str(submission_id),
                        "action": request.action,
                        "safety_rule_run_id": str(safety_run_id),
                    },
                    trace_id=_bounded_trace(trace_id),
                ),
            ),
            session_updates=_session_updates(
                current_stage="review",
                status="pending_review",
                pending_review=True,
                state_version=domain_state.state_version + 1,
                route="review_submission_staged",
                preserve_advance=_preserved_advance(meta),
            ),
            outbox_event_type="doctor.review_submitted.v1",
            outbox_payload={
                "session_id": session_id,
                "review_id": str(submission_id),
                "action": request.action,
                "input_state_version": domain_state.state_version,
                "output_state_version": domain_state.state_version + 1,
            },
        )
        await self._resume(
            session_id=session_id,
            submission_id=submission_id,
            shared_runtime=shared_runtime,
            allow_request_local_runtime=allow_request_local_runtime,
        )
        return await self._response(sid, submission_id, request)

    async def _resume(
        self,
        *,
        session_id: str,
        submission_id: uuid.UUID,
        shared_runtime: Any | None,
        allow_request_local_runtime: bool,
    ) -> None:
        config = make_run_config(session_id, graph_version=DEFAULT_GRAPH_VERSION)
        if shared_runtime is not None:
            await self._drive_checkpoint_resume(
                session_id=session_id,
                submission_id=submission_id,
                runner=shared_runtime.runner(timeout_seconds=120),
                graph=shared_runtime.graph,
                config=config,
            )
            return
        if not allow_request_local_runtime:
            raise ModelGatewayUnavailableError("shared LangGraph runtime is unavailable", retryable=True)
        async with postgres_checkpointer(get_settings().database_url) as saver:
            graph = build_main_graph(checkpointer=saver)
            await self._drive_checkpoint_resume(
                session_id=session_id,
                submission_id=submission_id,
                runner=GraphRunner(graph, timeout_seconds=120),
                graph=graph,
                config=config,
            )

    async def _drive_checkpoint_resume(
        self,
        *,
        session_id: str,
        submission_id: uuid.UUID,
        runner: GraphRunner,
        graph: Any,
        config: dict[str, Any],
    ) -> None:
        """Resume or rebuild one fail-closed Review interrupt.

        PostgreSQL is the clinical authority.  If it says a review is pending
        while the thread has no runnable task (for example, an older buggy
        node completed with an authority error), a deterministic reference-
        only graph turn recreates the interrupt before applying the already
        staged submission.
        """

        resume = {"review_submission_ref": str(submission_id)}
        for _attempt in range(2):
            snapshot = await graph.aget_state(config, subgraphs=True)
            if not snapshot.next and not snapshot.tasks:
                repository = PostgresDomainRepository(get_session_factory())
                domain_state = await repository.get_state(uuid.UUID(session_id))
                repair_run_id = uuid.uuid5(
                    uuid.NAMESPACE_URL,
                    f"xuanhu:review-checkpoint-repair:{submission_id}",
                )
                repair_state = default_state(
                    session_id=session_id,
                    command=XuanhuCommand.REVIEW.value,
                    command_id=f"review-checkpoint-repair:{submission_id}",
                    graph_version=DEFAULT_GRAPH_VERSION,
                    run_id=str(repair_run_id),
                )
                repair_state["domain_state_version"] = domain_state.state_version
                await runner.ainvoke(dict(repair_state), config=config)
            await runner.aresume(
                session_id=session_id,
                graph_version=DEFAULT_GRAPH_VERSION,
                resume=resume,
                config=config,
            )
            meta = await _session_meta(uuid.UUID(session_id))
            if not meta.pending_review:
                return
        raise InvalidStageTransitionError(
            message="Review resume did not close the pending interrupt",
            detail=f"session_id={session_id} review_id={submission_id}",
            retryable=True,
        )

    @staticmethod
    async def _response(
        session_id: uuid.UUID,
        review_id: uuid.UUID,
        request: ReviewRequest,
    ) -> ReviewResponse:
        factory = get_session_factory()
        async with factory() as db:
            session = await db.get(ConsultSession, session_id)
            review = await db.get(DoctorReview, review_id)
            if session is None or review is None or session.pending_review:
                raise InvalidStageTransitionError(
                    message="Review resume 未完成",
                    detail=f"session_id={session_id} review_id={review_id}",
                    retryable=True,
                )
            return ReviewResponse(
                session_id=str(session_id),
                action=request.action,
                current_stage=session.current_stage,
                status=session.status,
                pending_review=session.pending_review,
                review_id=str(review_id),
                state_version=session.state_version,
                original_formula=review.original_formula,
                formula_override=review.formula_override,
                feedback=review.feedback,
                safety_recheck=None,
                medical_record=None,
                updated_at=session.updated_at,
            )


__all__ = [
    "DOCTOR_REVIEW_ARTIFACT_TYPE",
    "LangGraphReviewService",
    "PreparedReview",
    "REVIEW_SUBMISSION_ARTIFACT_TYPE",
    "REVIEWED_FORMULA_ARTIFACT_TYPE",
    "REVIEWED_FORMULA_ATTEMPT_ARTIFACT_TYPE",
    "ReviewResumeRejected",
    "SAFETY_ARTIFACT_TYPE",
    "SAFETY_RECHECK_ATTEMPT_ARTIFACT_TYPE",
    "apply_review_resume",
    "load_prepared_review",
    "prepare_review_interrupt",
]
