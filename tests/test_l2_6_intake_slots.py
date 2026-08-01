"""2a: 粗槽位 schema 与阈值判定单测(决策 12/25)。"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.agent_runtime.intake_dimension_mapping import (
    dimension_slot_satisfied,
    slot_threshold_for,
)
from app.schemas.completeness import InquiryDimension
from app.schemas.intake import (
    DimensionSlotSnapshot,
    DimensionSlotValue,
    IntakeExtractionOutput,
    SlotCompleteness,
)


def test_slot_thresholds_derive_from_maturity_authority() -> None:
    # 决策 12 粗槽位: 单一真源 MATURITY_KEY_THRESHOLDS(寒热≥2/二便≥2/现病变化≥1),无阈值默认 1。
    assert slot_threshold_for(InquiryDimension.TEN_COLD_HEAT) == 2
    assert slot_threshold_for(InquiryDimension.TEN_STOOL_URINE) == 2
    assert slot_threshold_for(InquiryDimension.PRESENT_ILLNESS_CHANGE) == 1
    assert slot_threshold_for(InquiryDimension.TEN_SLEEP) == 1


def test_dimension_slot_satisfied_llm_signal_semantics() -> None:
    # 决策 25: LLM 主导 + 代码兜底。
    # partial → LLM 主导,槽位数达标也继续追问。
    assert dimension_slot_satisfied(InquiryDimension.TEN_COLD_HEAT, 2, llm_signal="partial") is False
    # complete → 代码复核阈值(LLM 不能突破下限)。
    assert dimension_slot_satisfied(InquiryDimension.TEN_COLD_HEAT, 2, llm_signal="complete") is True
    assert dimension_slot_satisfied(InquiryDimension.TEN_COLD_HEAT, 1, llm_signal="complete") is False
    # 无信号 → 代码兜底阈值判定。
    assert dimension_slot_satisfied(InquiryDimension.TEN_COLD_HEAT, 2) is True
    assert dimension_slot_satisfied(InquiryDimension.TEN_COLD_HEAT, 1) is False
    assert dimension_slot_satisfied(InquiryDimension.TEN_COLD_HEAT, 1, llm_signal="unknown") is False


def test_dimension_slot_snapshot_schema_contract() -> None:
    snapshot = DimensionSlotSnapshot(
        dimension="ten_questions.cold_heat",
        slots=(
            DimensionSlotValue(slot_name="aversion_cold", value="怕冷不明显"),
            DimensionSlotValue(slot_name="fever", value="不发烧"),
        ),
        completeness=SlotCompleteness.COMPLETE,
    )
    assert snapshot.dimension == "ten_questions.cold_heat"
    assert len(snapshot.slots) == 2
    assert snapshot.completeness is SlotCompleteness.COMPLETE

    # 模型不能自创维度/槽位名(程序定义枚举约束)。
    with pytest.raises(ValidationError):
        DimensionSlotSnapshot(dimension="symptoms.cold_heat", slots=())
    with pytest.raises(ValidationError):
        DimensionSlotValue(slot_name="chills!!", value="怕冷")
    with pytest.raises(ValidationError):
        DimensionSlotSnapshot(dimension="ten_questions.cold_heat", completeness="weird")


def test_intake_output_dimension_slots_optional() -> None:
    # 灰度关闭(默认): dimension_slots 为空,维持裸 fact_key 路径。
    output = IntakeExtractionOutput(decision="extracted", observations=())
    assert output.dimension_slots == ()

    # 灰度开启: 模型可产出槽位对象。
    output = IntakeExtractionOutput(
        decision="extracted",
        observations=(),
        dimension_slots=(
            DimensionSlotSnapshot(
                dimension="ten_questions.cold_heat",
                slots=(DimensionSlotValue(slot_name="aversion_cold", value="怕冷"),),
                completeness=SlotCompleteness.PARTIAL,
                missing_slots=("fever",),
            ),
        ),
    )
    assert output.dimension_slots[0].missing_slots == ("fever",)


def test_derive_dimension_slots_from_verified_observations() -> None:
    from datetime import UTC, datetime
    from uuid import uuid4

    from app.agent_runtime.intake_dimension_mapping import derive_dimension_slots
    from app.schemas.completeness import InquiryDimension
    from app.schemas.domain import ObservationSchema, ObservationStatus

    session_id = uuid4()
    source = uuid4()
    now = datetime.now(UTC)

    def mk(key: str, value: str) -> ObservationSchema:
        return ObservationSchema(
            observation_id=uuid4(),
            session_id=session_id,
            fact_key=key,
            value=value,
            source_message_id=source,
            status=ObservationStatus.ACTIVE,
            created_at=now,
        )

    facts = (
        mk("present_illness.chills", "怕冷不明显"),
        mk("present_illness.fever", "不发烧"),
        mk("chief_complaint.symptom", "受凉咳嗽一周"),
        mk("ten_questions.stool_urine", "大便正常"),
    )
    dimensions = frozenset(
        {
            InquiryDimension.TEN_COLD_HEAT,
            InquiryDimension.CHIEF_COMPLAINT_SYMPTOM,
            InquiryDimension.TEN_STOOL_URINE,
            InquiryDimension.TEN_SLEEP,
        }
    )
    snapshots = derive_dimension_slots(facts, dimensions=dimensions)
    by_dim = {item["dimension"]: item for item in snapshots}

    # 寒热采到 2 项 → 阈值 2 → complete。
    cold_heat = by_dim["ten_questions.cold_heat"]
    assert len(cold_heat["slots"]) == 2
    assert cold_heat["completeness"] == "complete"
    assert cold_heat["missing_slots"] == []

    # 主诉采到 1 项 → 阈值 1 → complete。
    assert by_dim["chief_complaint.symptom"]["completeness"] == "complete"

    # 二便只采到大便 1 项 → 阈值 2 → partial + 缺口。
    stool_urine = by_dim["ten_questions.stool_urine"]
    assert stool_urine["completeness"] == "partial"
    assert stool_urine["missing_slots"]

    # 睡眠无事实 → 产出 partial 快照(缺口提示,completeness 用它驱动追问)。
    sleep = by_dim["ten_questions.sleep"]
    assert sleep["completeness"] == "partial"
    assert sleep["missing_slots"]


def test_slot_based_covered_requires_slot_completeness() -> None:
    """2c: 灰度开启后 covered 认「粗槽位齐」——修复问题 6「一键即过」。

    - slot_based=False(默认/现状): 寒热任一键命中即 covered(一键即过)。
    - slot_based=True: 寒热需 keyset 内 2 项(怕冷+发热)才 covered;1 项不齐。
    - 无阈值维度(如睡眠,阈值 1)两个口径一致。
    """
    from uuid import uuid4

    from app.agent_runtime.completeness_policy import evaluate_completeness_policy
    from app.schemas.completeness import (
        CompletenessDomainSnapshot,
        CompletenessObservationFact,
        CompletenessPolicyInput,
        CompletenessProgress,
    )
    from app.schemas.domain import GateDecision
    from app.schemas.triage import TriageGateDetails, TriageGateResult

    session_id = uuid4()

    def run(slot_based: bool, *, with_fever: bool) -> tuple[bool, bool]:
        def fact(key: str) -> CompletenessObservationFact:
            return CompletenessObservationFact(
                observation_id=uuid4(),
                session_id=session_id,
                fact_key=key,
                value_fingerprint=key,
                normalized_code=None,
                status="active",
            )

        facts = [
            fact("chief_complaint.symptom"),
            fact("present_illness.chills"),
        ]
        if with_fever:
            facts.append(fact("present_illness.fever"))
        triage_gate = TriageGateResult(
            gate_name="triage",
            policy_version="triage-red-flag.v1",
            input_state_version=1,
            decision=GateDecision.PASSED,
            details=TriageGateDetails(
                disposition="continue",
                candidate_count=0,
                category_counts=(),
                rule_ids=(),
                rules=(),
                source_message_ids=(),
                risk_level="none",
            ),
        )
        domain_snapshot = CompletenessDomainSnapshot(
            session_id=session_id,
            state_version=1,
            observations=tuple(facts),
            safety_profile=None,
        )
        result = evaluate_completeness_policy(
            CompletenessPolicyInput(
                input_state_version=1,
                domain_snapshot=domain_snapshot,
                triage_gate=triage_gate,
                progress=CompletenessProgress(),
                slot_based=slot_based,
            )
        )
        return (
            InquiryDimension.TEN_COLD_HEAT in result.covered_dimensions,
            InquiryDimension.TEN_SLEEP in result.covered_dimensions,
        )

    # 现状口径(灰度关闭): 单条 chills 即 covered(一键即过)。
    cold_heat, _ = run(slot_based=False, with_fever=False)
    assert cold_heat is True
    # 槽位口径(灰度开启): 单条 chills 不齐(阈值 2)→ 不 covered。
    cold_heat, _ = run(slot_based=True, with_fever=False)
    assert cold_heat is False
    # 槽位口径: chills + fever 两项齐 → covered。
    cold_heat, _ = run(slot_based=True, with_fever=True)
    assert cold_heat is True
    # 无阈值维度(睡眠阈值 1)两个口径一致: 无事实均不 covered。
    _, sleep = run(slot_based=True, with_fever=False)
    assert sleep is False

    # 整维 canonical 键(ten_questions.cold_heat)命中 → 视为齐(模型合键输出,
    # 如"怕冷有一点点,不发烧,也没有出汗"合成一条),避免粗槽位数键漏判。
    facts = [
        CompletenessObservationFact(
            observation_id=uuid4(),
            session_id=session_id,
            fact_key="ten_questions.cold_heat",
            value_fingerprint="ten_questions.cold_heat",
            normalized_code=None,
            status="active",
        )
    ]
    result = evaluate_completeness_policy(
        CompletenessPolicyInput(
            input_state_version=1,
            domain_snapshot=CompletenessDomainSnapshot(
                session_id=session_id,
                state_version=1,
                observations=tuple(facts),
                safety_profile=None,
            ),
            triage_gate=TriageGateResult(
                gate_name="triage",
                policy_version="triage-red-flag.v1",
                input_state_version=1,
                decision=GateDecision.PASSED,
                details=TriageGateDetails(
                    disposition="continue",
                    candidate_count=0,
                    category_counts=(),
                    rule_ids=(),
                    rules=(),
                    source_message_ids=(),
                    risk_level="none",
                ),
            ),
            progress=CompletenessProgress(),
            slot_based=True,
        )
    )
    assert InquiryDimension.TEN_COLD_HEAT in result.covered_dimensions


def test_stagnation_cap_partial_vs_manual_handoff() -> None:
    """2d(决策 11): cap 到分流——缺非安全维度 → PARTIAL 推进;缺安全项 → STAGNATED 转人工。"""
    from uuid import uuid4

    from app.agent_runtime.completeness_policy import evaluate_completeness_policy
    from app.schemas.completeness import (
        CompletenessDomainSnapshot,
        CompletenessObservationFact,
        CompletenessPolicyInput,
        CompletenessProgress,
        CompletenessSafetyProfile,
    )
    from app.schemas.domain import GateDecision
    from app.schemas.triage import TriageGateDetails, TriageGateResult

    session_id = uuid4()

    def run(*keys: str, no_new_facts_rounds: int, safety_complete: bool = True) -> str:
        def fact(key: str) -> CompletenessObservationFact:
            return CompletenessObservationFact(
                observation_id=uuid4(),
                session_id=session_id,
                fact_key=key,
                value_fingerprint=key,
                normalized_code="male" if key == "patient.sex" else None,
                status="active",
            )

        safety_profile = (
            CompletenessSafetyProfile(
                session_id=session_id,
                allergy_collection_status="explicitly_none",
                medications_collection_status="explicitly_none",
                major_conditions_collection_status="explicitly_none",
            )
            if safety_complete
            else None
        )

        triage_gate = TriageGateResult(
            gate_name="triage",
            policy_version="triage-red-flag.v1",
            input_state_version=1,
            decision=GateDecision.PASSED,
            details=TriageGateDetails(
                disposition="continue",
                candidate_count=0,
                category_counts=(),
                rule_ids=(),
                rules=(),
                source_message_ids=(),
                risk_level="none",
            ),
        )
        result = evaluate_completeness_policy(
            CompletenessPolicyInput(
                input_state_version=1,
                domain_snapshot=CompletenessDomainSnapshot(
                    session_id=session_id,
                    state_version=1,
                    observations=tuple(fact(key) for key in keys),
                    safety_profile=safety_profile,
                ),
                triage_gate=triage_gate,
                progress=CompletenessProgress(no_new_facts_rounds=no_new_facts_rounds),
            )
        )
        return result.disposition.value

    # cap 到(no_new_facts_rounds≥2)且缺非安全维度(寒热未采)→ PARTIAL 推进。
    # (male 性别使妊娠/哺乳不适用,安全三项已齐,missing 只剩非安全维度)
    assert run("chief_complaint.symptom", "patient.sex", no_new_facts_rounds=2) == "partial"
    # cap 到且安全项(过敏)缺失 → STAGNATED 转人工(铁律 9)。
    assert (
        run("chief_complaint.symptom", "present_illness.chills", "present_illness.fever",
            "patient.sex", no_new_facts_rounds=2, safety_complete=False)
        == "stagnated"
    )
    # 未到 cap → 正常 INCOMPLETE 继续追问。
    assert run("chief_complaint.symptom", "patient.sex", no_new_facts_rounds=0) == "incomplete"


def test_derive_slot_context_rows_projection(monkeypatch: pytest.MonkeyPatch) -> None:
    """3a: 下游投影槽位对象(问题 22)——灰度开启时辨证/开方输入为规整维度行。"""
    from datetime import UTC, datetime
    from uuid import uuid4

    from app.agent_runtime.intake_dimension_mapping import derive_slot_context_rows
    from app.schemas.domain import ObservationSchema, ObservationStatus

    session_id = uuid4()
    now = datetime.now(UTC)
    facts = (
        ObservationSchema(
            observation_id=uuid4(),
            session_id=session_id,
            fact_key="present_illness.chills",
            value="怕冷不明显",
            normalized_value=None,
            source_message_id=uuid4(),
            status=ObservationStatus.ACTIVE,
            created_at=now,
        ),
        ObservationSchema(
            observation_id=uuid4(),
            session_id=session_id,
            fact_key="present_illness.fever",
            value="不发烧",
            normalized_value=None,
            source_message_id=uuid4(),
            status=ObservationStatus.ACTIVE,
            created_at=now,
        ),
        ObservationSchema(
            observation_id=uuid4(),
            session_id=session_id,
            fact_key="ten_questions.cold_heat",
            value="怕冷,不发烧",
            normalized_value=None,
            source_message_id=uuid4(),
            status=ObservationStatus.ACTIVE,
            created_at=now,
        ),
        ObservationSchema(
            observation_id=uuid4(),
            session_id=session_id,
            fact_key="chief_complaint.symptom",
            value="咳嗽三天",
            normalized_value=None,
            source_message_id=uuid4(),
            status=ObservationStatus.ACTIVE,
            created_at=now,
        ),
        # 非 active 不投影
        ObservationSchema(
            observation_id=uuid4(),
            session_id=session_id,
            fact_key="ten_questions.sleep",
            value="差",
            normalized_value=None,
            source_message_id=uuid4(),
            status=ObservationStatus.RETRACTED,
            supersedes_observation_id=uuid4(),
            created_at=now,
        ),
    )
    rows = derive_slot_context_rows(
        facts,
        dimensions=frozenset(
            {
                InquiryDimension.TEN_COLD_HEAT,
                InquiryDimension.CHIEF_COMPLAINT_SYMPTOM,
                InquiryDimension.TEN_SLEEP,
            }
        ),
        state_version=3,
        session_id=session_id,
    )
    by_key = {row["fact_key"]: row for row in rows}
    # 寒热维度一行: fact_key=维度枚举(无漂移键),value=槽位快照(JSON-safe)。
    cold_heat = by_key["ten_questions.cold_heat"]
    assert cold_heat["value"]["dimension"] == "ten_questions.cold_heat"
    assert len(cold_heat["value"]["slots"]) >= 1
    assert cold_heat["state_version"] == 3
    # 主诉行存在。
    assert "chief_complaint.symptom" in by_key
    # 非 active 观察不产行。
    assert "ten_questions.sleep" not in by_key
    # 行可 JSON 序列化(下游 prompt 投影用)。
    import json as _json

    for row in rows:
        _json.dumps(row, ensure_ascii=False)


def test_syndrome_context_slot_projection_when_gray_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    """3a review should-fix: 灰度开启时 syndrome_draft 的 context_observations 投影槽位对象。"""
    from datetime import UTC, datetime
    from uuid import uuid4

    from app.agent_runtime.reducer import DomainState
    from app.agents.syndrome_draft import _context_from_domain_state
    from app.core.config import get_settings
    from app.schemas.domain import ObservationSchema, ObservationStatus

    monkeypatch.setenv("XUANHU_INTAKE_SLOT_PATH_ENABLED", "true")
    get_settings.cache_clear()

    session_id = uuid4()
    now = datetime.now(UTC)
    state = DomainState(
        session_id=session_id,
        state_version=5,
        observations=(
            ObservationSchema(
                observation_id=uuid4(),
                session_id=session_id,
                fact_key="present_illness.chills",
                value="怕冷不明显",
                normalized_value=None,
                source_message_id=uuid4(),
                status=ObservationStatus.ACTIVE,
                created_at=now,
            ),
            ObservationSchema(
                observation_id=uuid4(),
                session_id=session_id,
                fact_key="present_illness.fever",
                value="不发烧",
                normalized_value=None,
                source_message_id=uuid4(),
                status=ObservationStatus.ACTIVE,
                created_at=now,
            ),
        ),
    )
    try:
        rows = _context_from_domain_state(state)
        assert len(rows) >= 1
        cold_heat_rows = [row for row in rows if row.fact_key == "ten_questions.cold_heat"]
        assert cold_heat_rows, "槽位投影应产出寒热维度行"
        value = cold_heat_rows[0].value
        assert value["dimension"] == "ten_questions.cold_heat"
        assert len(value["slots"]) == 2
    finally:
        monkeypatch.delenv("XUANHU_INTAKE_SLOT_PATH_ENABLED")
        get_settings.cache_clear()
