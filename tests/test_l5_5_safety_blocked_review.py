"""L5-5 安全拦截（safety_rule_blocked）后的医师修正路径集成测试。

覆盖问题 17 的修复：安全引擎以 HIGH 剂量超限拦截处方后，会话停在
blocked/safety_rule_blocked。医生此前无法 review（review 端点只接受
review/pending_review），recover retry_current_stage 又用同一份超限方子
重跑 safety 而无限循环——唯一出口是回滚到 inquiry 重问。

修复后：
- blocked(safety_rule_blocked) 会话可被 _prepared_from_current 准备；
- confirm 仍被拒（绕过确定性安全门）；
- modify（合规剂量）→ 二次安全审核 → record → advance 生成病历。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import cast

import pytest
from sqlalchemy import select

from app.agent_runtime.config import DEFAULT_GRAPH_VERSION
from app.agent_runtime.formula_consistency import FORMULA_CONSISTENCY_POLICY_VERSION
from app.agent_runtime.reducer import DomainDelta
from app.agent_runtime.repository import (
    ArtifactPayloadRecord,
    ArtifactPayloadSpec,
    PostgresDomainRepository,
    SafetyRuleRunSpec,
    artifact_payload_digest,
)
from app.core.config import get_settings
from app.core.exceptions import SafetyReviewBlockedError
from app.db.session import get_session_factory
from app.models.consult import ConsultSession
from app.models.domain import GraphRun, SafetyProfile
from app.models.knowledge import DosageUnit, Herb
from app.models.review import DoctorReview
from app.models.safety import SafetyRuleRun
from app.schemas.agent import FormulaResult, HerbDose, SafetyIssue, SafetyRuleResult
from app.schemas.domain import (
    ArtifactRevisionSchema,
    ArtifactStatus,
    GateDecision,
    GateResultSchema,
    SafetyProfileSchema,
)
from app.schemas.formula import (
    FORMULA_EVIDENCE_MODE,
    FormulaComposition,
    FormulaDraft,
    FormulaDraftDecision,
    FormulaFactClaim,
    HerbItem,
)
from app.schemas.review import FormulaOverride, HerbOverrideItem, ReviewRequest
from app.services.langgraph_review import (
    FORMULA_ARTIFACT_TYPE,
    FORMULA_PAYLOAD_SCHEMA_VERSION,
    SAFETY_ARTIFACT_TYPE,
    SAFETY_PAYLOAD_SCHEMA_VERSION,
    FormulaAuthority,
    LangGraphReviewService,
    PreparedReview,
    _formula_ref,
    _patient_info_from_domain,
    _prepared_from_current,
    _session_updates,
    _verification_context,
)

pytestmark = [pytest.mark.integration, pytest.mark.asyncio(loop_scope="module")]


@dataclass(frozen=True, slots=True)
class _SeededBlocked:
    session_id: uuid.UUID
    herb_name: str
    unit_name: str
    state_version: int


def _record(
    *,
    session_id: uuid.UUID,
    artifact_id: uuid.UUID,
    artifact_type: str,
    revision: int,
    input_state_version: int,
    produced_by_run_id: uuid.UUID,
    payload_schema_version: str,
    payload: dict[str, object],
) -> ArtifactPayloadRecord:
    digest = artifact_payload_digest(payload_schema_version, payload)
    return ArtifactPayloadRecord(
        row_id=uuid.uuid4(),
        artifact_revision_row_id=uuid.uuid4(),
        session_id=session_id,
        artifact_id=artifact_id,
        artifact_type=artifact_type,
        revision=revision,
        input_state_version=input_state_version,
        status="current",
        produced_by_run_id=produced_by_run_id,
        payload_schema_version=payload_schema_version,
        payload=payload,
        content_digest=digest,
    )


async def _seed_blocked_safety() -> _SeededBlocked:
    """Seed a session whose agent formula was blocked by a HIGH dose limit.

    Domain versions: v1 session + profile, v2 formula artifact, v3 failed
    safety authority; session row lands at blocked / safety_rule_blocked.
    """
    factory = get_session_factory()
    session_id = uuid.uuid4()
    suffix = session_id.hex[:6]
    herb_name = f"blockedherb{suffix}"
    unit_name = f"bu{suffix}"
    safety_rule_run_id = uuid.uuid4()
    # 预热 checkpointer：其 setup() 执行 CREATE INDEX CONCURRENTLY，若有并发
    # 打开的 idle transaction 会永久阻塞。先于任何服务会话建好索引（生产环境
    # 索引在首次使用后已存在，只有全新测试库会触发）。
    from app.agent_runtime.checkpoint import postgres_checkpointer

    async with postgres_checkpointer(get_settings().database_url):
        pass
    async with factory() as db, db.begin():
        db.add(
            ConsultSession(
                id=session_id,
                patient_info={"name": "integration-only"},
                chief_complaint="integration-only",
                current_stage="blocked",
                status="blocked",
                agent_runtime="langgraph",
                pending_review=False,
                rollback_counts={},
                state_snapshot={"agent_runtime": "langgraph"},
                state_version=3,
                recovery_status="manual_required",
                blocked_reason="safety_rule_blocked",
            )
        )
        db.add(
            SafetyProfile(
                id=uuid.uuid4(),
                session_id=session_id,
                allergy_collection_status="explicitly_none",
                allergens=None,
                pregnancy_collection_status="explicitly_none",
                pregnancy_value=None,
                lactation_collection_status="explicitly_none",
                lactation_value=None,
                medications_collection_status="explicitly_none",
                medications=None,
                major_conditions_collection_status="explicitly_none",
                major_conditions=None,
                contraindications_collection_status="explicitly_none",
                contraindications=None,
            )
        )
        db.add(
            Herb(
                id=uuid.uuid4(),
                name=herb_name,
                aliases=[],
                meridians=[],
                contraindications=[],
                eighteen_incompatibilities=[],
                nineteen_fears=[],
                pregnancy_contraindication="none",
                incompatibilities=[],
                max_dose=9,
                doc_text="integration-only herb",
            )
        )
        db.add(
            DosageUnit(
                id=uuid.uuid4(),
                unit_name=unit_name,
                aliases=[],
                to_grams=1,
                conversion_type="standard",
                is_standard=True,
                enabled=True,
            )
        )

    repository = PostgresDomainRepository(factory)
    state = await repository.get_state(session_id)

    # ---- v2: formula artifact (12g, over the 9g limit) ----
    formula_run_id = uuid.uuid4()
    artifact_id = uuid.uuid5(uuid.NAMESPACE_URL, f"xuanhu:{FORMULA_ARTIFACT_TYPE}:{session_id}")
    fact_id = uuid.uuid4()
    basis = (FormulaFactClaim(claim="integration authority", fact_ids=(fact_id,)),)
    formula = FormulaComposition(
        name="integration formula",
        composition=(HerbItem(herb=herb_name, dose=12, unit=unit_name),),
        rationale="integration-only blocked formula",
        basis=basis,
    )
    draft = FormulaDraft(
        decision=FormulaDraftDecision.COMPLETED,
        base_formula=formula,
        candidate_formula=formula,
        rationale="integration-only blocked formula",
        confidence=0.5,
        evidence_mode=FORMULA_EVIDENCE_MODE,
        review_required=True,
    )
    formula_payload: dict[str, object] = {
        "kind": FORMULA_ARTIFACT_TYPE,
        "output": draft.model_dump(mode="json"),
        "input_payload": {"state_version": state.state_version},
        "run_spec": {},
        "run_artifact": {},
        "verification": {"passed": True},
        "consistency": {"passed": True},
    }
    formula_digest = artifact_payload_digest(FORMULA_PAYLOAD_SCHEMA_VERSION, formula_payload)
    artifact = ArtifactRevisionSchema(
        artifact_id=artifact_id,
        artifact_type=FORMULA_ARTIFACT_TYPE,
        revision=1,
        session_id=session_id,
        input_state_version=state.state_version,
        status=ArtifactStatus.CURRENT,
        produced_by_run_id=formula_run_id,
        created_at=datetime.now(UTC),
    )
    delta = DomainDelta(
        delta_id=uuid.uuid4(),
        run_id=formula_run_id,
        session_id=session_id,
        expected_state_version=state.state_version,
        artifact_revisions=(artifact,),
    )
    await repository.commit(
        delta,
        _verification_context(
            delta,
            state,
            stage="formula",
            idempotency_key=f"blocked-integration-formula:{session_id}",
            trace_id=f"blocked-seed-{suffix}",
        ),
        graph_version=DEFAULT_GRAPH_VERSION,
        gate_results=(
            GateResultSchema(
                gate_name="formula_consistency",
                policy_version=FORMULA_CONSISTENCY_POLICY_VERSION,
                input_state_version=state.state_version,
                decision=GateDecision.PASSED,
                details={"artifact_digest": formula_digest},
            ),
        ),
        artifact_payloads=(
            ArtifactPayloadSpec(
                session_id=session_id,
                artifact_id=artifact_id,
                revision=1,
                payload_schema_version=FORMULA_PAYLOAD_SCHEMA_VERSION,
                payload=formula_payload,
                content_digest=formula_digest,
            ),
        ),
        session_updates=_session_updates(
            current_stage="safety",
            status="active",
            pending_review=False,
            state_version=state.state_version + 1,
            route="ready_for_safety",
        ),
        outbox_event_type="integration.formula_ready.v1",
        outbox_payload={"session_id": str(session_id)},
    )

    # ---- v3: failed safety authority (HIGH dose limit) ----
    state = await repository.get_state(session_id)
    safety_run_id = uuid.uuid4()
    formula_result = FormulaResult(
        name="integration formula",
        composition=[HerbDose(herb=herb_name, dose=12, unit=unit_name)],
        rationale="integration-only blocked formula",
    )
    result = SafetyRuleResult(
        passed=False,
        issues=[
            SafetyIssue(
                type="dose_limit",
                severity="high",
                herbs=[herb_name],
                rule_source="《中国药典》",
                suggestion=f"「{herb_name}」剂量 12.0g 超过上限 9.0g（一般超量）。请调整剂量。",
            )
        ],
        normalized_formula=formula_result,
        rule_version="v1.0.0",
        execution_order=["normalize", "convert_dose", "dose_limit"],
    )
    formula_authority = FormulaAuthority(
        _record(
            session_id=session_id,
            artifact_id=artifact_id,
            artifact_type=FORMULA_ARTIFACT_TYPE,
            revision=1,
            input_state_version=1,
            produced_by_run_id=formula_run_id,
            payload_schema_version=FORMULA_PAYLOAD_SCHEMA_VERSION,
            payload=formula_payload,
        ),
        formula_result,
    )
    safety_artifact_id = uuid.uuid5(uuid.NAMESPACE_URL, f"xuanhu:{SAFETY_ARTIFACT_TYPE}:{session_id}")
    safety_payload: dict[str, object] = {
        "kind": SAFETY_ARTIFACT_TYPE,
        "formula_ref": _formula_ref(formula_authority),
        "result": result.model_dump(mode="json"),
        "safety_rule_run_id": str(safety_rule_run_id),
        "agent_run_id": None,
        "trace_id": f"blocked-seed-{suffix}",
    }
    safety_digest = artifact_payload_digest(SAFETY_PAYLOAD_SCHEMA_VERSION, safety_payload)
    safety_artifact = ArtifactRevisionSchema(
        artifact_id=safety_artifact_id,
        artifact_type=SAFETY_ARTIFACT_TYPE,
        revision=1,
        session_id=session_id,
        input_state_version=state.state_version,
        status=ArtifactStatus.CURRENT,
        produced_by_run_id=safety_run_id,
        created_at=datetime.now(UTC),
    )
    patient_info = _patient_info_from_domain(
        cast(SafetyProfileSchema, state.safety_profile),
        observations=state.observations,
    )
    delta = DomainDelta(
        delta_id=uuid.uuid4(),
        run_id=safety_run_id,
        session_id=session_id,
        expected_state_version=state.state_version,
        artifact_revisions=(safety_artifact,),
    )
    await repository.commit(
        delta,
        _verification_context(
            delta,
            state,
            stage="safety",
            idempotency_key=f"blocked-integration-safety:{session_id}",
            trace_id=f"blocked-seed-{suffix}",
        ),
        graph_version=DEFAULT_GRAPH_VERSION,
        gate_results=(
            GateResultSchema(
                gate_name="safety_rule_engine",
                policy_version="safety-rule-engine.v1",
                input_state_version=state.state_version,
                decision=GateDecision.FAILED,
                details={"artifact_digest": safety_digest, "issue_count": 1},
            ),
        ),
        artifact_payloads=(
            ArtifactPayloadSpec(
                session_id=session_id,
                artifact_id=safety_artifact_id,
                revision=1,
                payload_schema_version=SAFETY_PAYLOAD_SCHEMA_VERSION,
                payload=safety_payload,
                content_digest=safety_digest,
            ),
        ),
        safety_rule_runs=(
            SafetyRuleRunSpec(
                safety_rule_run_id=safety_rule_run_id,
                session_id=session_id,
                formula_source="agent_output",
                passed=False,
                issues=[cast(dict[str, object], item.model_dump(mode="json")) for item in result.issues],
                formula_snapshot=cast(dict[str, object], formula_result.model_dump(mode="json")),
                normalized_formula=cast(dict[str, object], result.normalized_formula.model_dump(mode="json")),
                patient_snapshot=cast(dict[str, object], patient_info.model_dump(mode="json", exclude={"name"})),
                rule_version="v1.0.0",
                trace_id=f"blocked-seed-{suffix}",
            ),
        ),
        session_updates=_session_updates(
            current_stage="blocked",
            status="blocked",
            pending_review=False,
            state_version=state.state_version + 1,
            route="safety_blocked",
            blocked_reason="safety_rule_blocked",
        ),
        outbox_event_type="safety.blocked.v1",
        outbox_payload={"session_id": str(session_id), "passed": False, "issue_count": 1},
    )
    return _SeededBlocked(
        session_id=session_id,
        herb_name=herb_name,
        unit_name=unit_name,
        state_version=state.state_version + 1,
    )


@pytest.mark.asyncio
async def test_blocked_safety_prepare_allows_review_but_blocks_confirm() -> None:
    seed = await _seed_blocked_safety()
    prepared = await _prepared_from_current(seed.session_id)
    assert isinstance(prepared, PreparedReview)
    assert prepared.from_blocked_safety is True
    assert prepared.safety_result.passed is False
    assert prepared.state_version == seed.state_version

    # confirm 绕过安全门，必须拒绝
    factory = get_session_factory()
    async with factory() as db:
        with pytest.raises(SafetyReviewBlockedError):
            await LangGraphReviewService(db).review(
                str(seed.session_id),
                ReviewRequest(action="confirm"),
                doctor_id="blocked-doctor",
                trace_id="blocked-confirm",
                x_state_version=seed.state_version,
                idempotency_key=f"blocked-confirm:{uuid.uuid4()}",
                shared_runtime=None,
                allow_request_local_runtime=True,
            )


@pytest.mark.asyncio
async def test_blocked_safety_modify_compliant_override_resolves_to_record() -> None:
    seed = await _seed_blocked_safety()
    factory = get_session_factory()

    async with factory() as db:
        response = await LangGraphReviewService(db).review(
            str(seed.session_id),
            ReviewRequest(
                action="modify",
                formula_override=FormulaOverride(
                    name="integration override",
                    composition=[
                        HerbOverrideItem(herb=seed.herb_name, dose=9, unit=seed.unit_name),
                    ],
                    rationale="dose reduced to the 9g limit",
                ),
                feedback="dose adjusted",
            ),
            doctor_id="blocked-doctor",
            trace_id="blocked-modify",
            x_state_version=seed.state_version,
            idempotency_key=f"blocked-modify:{uuid.uuid4()}",
            shared_runtime=None,
            allow_request_local_runtime=True,
        )

    assert response.current_stage == "record"
    assert response.pending_review is False

    async with factory() as db:
        session = await db.get(ConsultSession, seed.session_id)
        assert session is not None and session.current_stage == "record"
        assert session.status == "active" and not session.pending_review
        assert session.blocked_reason is None
        reviews = await db.scalars(
            select(DoctorReview).where(DoctorReview.session_id == seed.session_id)
        )
        assert len(list(reviews)) == 1
        safety_runs = list(
            await db.scalars(
                select(SafetyRuleRun).where(SafetyRuleRun.session_id == seed.session_id)
            )
        )
        sources = {run.formula_source for run in safety_runs}
        assert "agent_output" in sources
        assert "doctor_override" in sources
        overrides = [run for run in safety_runs if run.formula_source == "doctor_override"]
        assert overrides and overrides[0].passed is True


@pytest.mark.asyncio
async def test_blocked_safety_modify_over_limit_override_stays_reviewable() -> None:
    """医生 modify 仍超限 → 回到 pending_review，且可再次 modify 修正（不死锁）。"""
    seed = await _seed_blocked_safety()
    factory = get_session_factory()

    async with factory() as db:
        with pytest.raises(SafetyReviewBlockedError):
            await LangGraphReviewService(db).review(
                str(seed.session_id),
                ReviewRequest(
                    action="modify",
                    formula_override=FormulaOverride(
                        name="integration override",
                        composition=[
                            HerbOverrideItem(herb=seed.herb_name, dose=12, unit=seed.unit_name),
                        ],
                        rationale="still over the limit",
                    ),
                    feedback="dose unchanged",
                ),
                doctor_id="blocked-doctor",
                trace_id="blocked-modify-fail",
                x_state_version=seed.state_version,
                idempotency_key=f"blocked-modify-fail:{uuid.uuid4()}",
                shared_runtime=None,
                allow_request_local_runtime=True,
            )

    async with factory() as db:
        session = await db.get(ConsultSession, seed.session_id)
        assert session is not None
        assert session.current_stage == "review" and session.pending_review

    # 修正态下仍可准备（from_blocked_safety 继承底层拦截）
    prepared = await _prepared_from_current(seed.session_id)
    assert prepared.from_blocked_safety is True

    async with factory() as db:
        response = await LangGraphReviewService(db).review(
            str(seed.session_id),
            ReviewRequest(
                action="modify",
                formula_override=FormulaOverride(
                    name="integration override",
                    composition=[
                        HerbOverrideItem(herb=seed.herb_name, dose=9, unit=seed.unit_name),
                    ],
                    rationale="dose reduced to the 9g limit",
                ),
                feedback="dose adjusted",
            ),
            doctor_id="blocked-doctor",
            trace_id="blocked-modify-retry",
            x_state_version=session.state_version,
            idempotency_key=f"blocked-modify-retry:{uuid.uuid4()}",
            shared_runtime=None,
            allow_request_local_runtime=True,
        )
    assert response.current_stage == "record"
